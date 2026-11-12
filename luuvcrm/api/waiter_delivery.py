"""Physical delivery confirmations and manager-controlled removal of delivered food.

Delivery never submits a bill, takes payment or changes stock. The order row
lock serializes confirmation, edits and approval decisions; its stable item /
takeaway keys survive the POS's normal child-row rebuilds.
"""
import base64
import hashlib
import io
import json
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime
from luuvcrm import branch, roles, activity

FIELD='luuv_delivery_state'
EVIDENCE='_delivery_evidence'


def _json(raw):
    value=frappe.parse_json(raw) if isinstance(raw,str) and raw.strip() else raw
    return value if isinstance(value,dict) else {}


def _key(item,takeaway=0):return json.dumps([item,cint(takeaway)],separators=(',',':'))


def groups(rows,order_type=None):
    result={}
    for row in rows or []:
        item=row.get('item');ta=cint(row.get('takeaway'))
        if row.get('takeaway') is None and order_type=='Take Away':ta=1
        key=_key(item,ta)
        group=result.setdefault(key,dict(key=key,item=item,name=row.get('item_name') or item,
            takeaway=ta,qty=0,notes=[]))
        group['qty']+=flt(row.get('qty'))
        note=row.get('notes')
        if note and note not in group['notes']:group['notes'].append(note)
    return result


def state(name,lock=False):
    if not frappe.db.has_column('POS Order',FIELD):return dict(revision=0,delivered={},events=[])
    rows=frappe.db.sql('SELECT `'+FIELD+'` FROM `tabPOS Order` WHERE name=%s'+(' FOR UPDATE' if lock else ''),(name,))
    data=_json(rows[0][0]) if rows else {}
    return dict(revision=cint(data.get('revision')),delivered=data.get('delivered') or {},events=data.get('events') or [])


def _order(name):
    roles.require_waiter();branch.require_access('POS Order',name)
    return frappe.get_doc('POS Order',name,for_update=True)


def _impact(saved,proposed):
    return {key:flt(qty)-flt(proposed.get(key,{}).get('qty')) for key,qty in saved['delivered'].items()
            if flt(qty)>flt(proposed.get(key,{}).get('qty'))+1e-7}


def _save(doc,saved,event):
    saved['revision']+=1;event.update(by=frappe.session.user,when=str(now_datetime()),revision=saved['revision'])
    saved['events'].append(event)
    frappe.db.set_value('POS Order',doc.name,FIELD,frappe.as_json(saved))
    frappe.get_doc(dict(doctype='Version',ref_doctype='POS Order',docname=doc.name,
        data=frappe.as_json({'waiter_delivery':event}))).insert(ignore_permissions=True)
    frappe.publish_realtime('waiter_delivery',{'order':doc.name,'restaurant':doc.get(branch.FIELD)},after_commit=True)


def _view(doc,saved=None):
    saved=saved or state(doc.name)
    lines=list(groups(doc.get('items'),doc.order_type).values())
    for row in lines:
        row['delivered']=min(row['qty'],flt(saved['delivered'].get(row['key'])))
        row['remaining']=max(0,row['qty']-row['delivered'])
    users={e.get('by') for e in saved['events'] if e.get('by')}
    names={u:frappe.get_value('User',u,'full_name') or u for u in users}
    history=[dict(e,by_name=names.get(e.get('by'),e.get('by') or '')) for e in saved['events']]
    return dict(name=doc.name,table=doc.restaurant_table or '',order_type=doc.order_type or '',
        kitchen_status=doc.kitchen_status or '',paid=bool(doc.pos_invoice),closed=doc.docstatus!=0,
        customer=doc.customer_name or '',created=str(doc.creation),revision=saved['revision'],
        lines=lines,history=history,complete=bool(lines) and all(r['remaining']<=0 for r in lines))


@frappe.whitelist()
def orders(day=None):
    roles.require_waiter();day=getdate(day or frappe.utils.today())
    names=frappe.get_list('POS Order',filters={branch.FIELD:branch.viewing(),'docstatus':['!=',2],
        'creation':['between',[str(day),str(day)+' 23:59:59.999999']]},pluck='name',
        order_by='creation desc',limit_page_length=0)
    return dict(orders=[_view(frappe.get_doc('POS Order',name)) for name in names],day=str(day),restaurant=branch.viewing())


@frappe.whitelist(methods=['POST'])
def confirm(order_name,lines,expected_revision,request_id):
    doc=_order(order_name)
    if doc.docstatus==2:frappe.throw(_('A cancelled order cannot be delivered'))
    from luuvcrm.api.orders import _require_cus_call_released
    _require_cus_call_released(doc)
    saved=state(doc.name,True)
    lines=frappe.parse_json(lines) if isinstance(lines,str) else lines
    if not isinstance(lines,list) or not lines:frappe.throw(_('Choose the quantities you delivered'))
    if not isinstance(request_id,str) or not 16<=len(request_id)<=80:frappe.throw(_('Reload the delivery confirmation'))
    digest=hashlib.sha256(frappe.as_json(lines).encode()).hexdigest()
    for event in saved['events']:
        if event.get('request_id')==request_id:
            if event.get('digest')!=digest:frappe.throw(_('This confirmation was already used'))
            return dict(ok=True,already_processed=True,order=_view(doc,saved))
    if cint(expected_revision)!=saved['revision']:frappe.throw(_('Delivery changed on another screen. Refresh and check the quantities'))
    current=groups(doc.get('items'),doc.order_type);confirmed=[];seen=set()
    for line in lines:
        if not isinstance(line,dict):frappe.throw(_('Choose valid delivery quantities'))
        key=line.get('key');qty=flt(line.get('qty'))
        if key not in current or key in seen or qty<=0 or not qty.is_integer():frappe.throw(_('Choose valid whole delivery quantities'))
        seen.add(key);row=current[key]
        if qty+flt(saved['delivered'].get(key))>row['qty']+1e-7:frappe.throw(_('Delivery quantity exceeds the remaining items. Refresh this order'))
        saved['delivered'][key]=flt(saved['delivered'].get(key))+qty
        confirmed.append(dict(key=key,item=row['item'],name=row['name'],takeaway=row['takeaway'],qty=qty))
    event=dict(kind='confirmed',request_id=request_id,digest=digest,lines=confirmed)
    _save(doc,saved,event)
    activity.log_activity('order.delivery_confirmed',label='Delivery confirmed: '+doc.name,
        ref_doctype='POS Order',ref_name=doc.name,detail=event)
    return dict(ok=True,order=_view(doc,saved))


def guard(doc,method=None):
    """Enforce the protected quantities on every normal document save/delete."""
    if doc.is_new():
        if doc.get(FIELD):frappe.throw(_('Delivery must be confirmed through Waiter 2.0'))
        return
    saved=state(doc.name,True)
    if method=='on_trash' and saved['events']:
        frappe.throw(_('Orders with delivery history must be retained'))
    if method not in ('on_trash','before_cancel') and _json(doc.get(FIELD)) not in ({},saved):
        # Frappe serializes an untouched empty state as blank on older orders.
        raw=_json(doc.get(FIELD))
        if raw!=saved:frappe.throw(_('Delivery history changed. Refresh this order'))
    proposed={} if method in ('on_trash','before_cancel') or doc.docstatus==2 else groups(doc.get('items'),doc.order_type)
    impacted=_impact(saved,proposed)
    allowed=getattr(frappe.local,'delivery_approved_change',None)
    if impacted and not (allowed and allowed['order']==doc.name and allowed['counts']=={k:v['qty'] for k,v in proposed.items()}):
        frappe.throw(_('Delivered items are locked. Request manager approval with a comment and photo in Waiter 2.0 or the POS'))
    # Even clearing the field must not erase a recorded delivery.
    if saved['events'] and method not in ('on_trash','before_cancel') and _json(doc.get(FIELD))!=saved:
        frappe.throw(_('Delivery history cannot be edited or cleared'))


def guard_cancel(doc):guard(doc,'before_cancel')


def _photo(photo,order_name):
    if not photo or not frappe.db.exists('File',photo):frappe.throw(_('Attach a photo of the delivered item'))
    file=frappe.get_doc('File',photo)
    if not file.is_private or file.attached_to_doctype!='POS Order' or file.attached_to_name!=order_name:
        frappe.throw(_('Use a photo uploaded for this order'))
    from PIL import Image
    try:
        image=Image.open(io.BytesIO(file.get_content()));image.verify()
        if image.format not in ('JPEG','PNG','WEBP'):raise ValueError('Unsupported image')
    except Exception:frappe.throw(_('The attachment must be a valid photo'))
    return file


@frappe.whitelist(methods=['POST'])
def upload_photo(order_name,content):
    doc=_order(order_name)
    if len(content or '')>7*1024*1024:frappe.throw(_('Choose a photo smaller than 5 MB'))
    from PIL import Image,ImageOps
    try:
        raw=base64.b64decode(content,validate=True)
        if len(raw)>5*1024*1024:raise ValueError('Too large')
        image=Image.open(io.BytesIO(raw))
        if image.format not in ('JPEG','PNG','WEBP') or image.width*image.height>20000000:raise ValueError('Unsupported photo')
        image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((1800,1800))
        output=io.BytesIO();image.save(output,format='JPEG',quality=85)
    except Exception:frappe.throw(_('Choose a valid JPG, PNG or WebP photo under 5 MB'))
    from frappe.utils.file_manager import save_file
    file=save_file('delivery-'+frappe.generate_hash(length=12)+'.jpg',output.getvalue(),
        'POS Order',doc.name,is_private=1)
    return dict(photo=file.name,url=file.file_url)


@frappe.whitelist()
def removal_check(order_name,items=None,action='update_items'):
    doc=_order(order_name);saved=state(doc.name,True)
    values=frappe.parse_json(items) if isinstance(items,str) else items
    if action not in ('update_items','void'):return dict(required=False)
    proposed=groups(values or [],doc.order_type) if action=='update_items' else {}
    return dict(required=bool(_impact(saved,proposed)),revision=saved['revision'])


def prepare_request(doc,action,payload,data):
    """Create server-owned evidence; a client cannot claim a delivery approval."""
    payload.pop(EVIDENCE,None)
    if action not in ('update_items','void'):return payload,None
    saved=state(doc.name,True)
    proposed=groups(payload.get('items') or [],doc.order_type) if action=='update_items' else {}
    impacted=_impact(saved,proposed)
    if not impacted:return payload,None
    if not roles.has_any(roles.POS_ROLES) and action=='update_items':
        # Floor staff can request quantity reductions, not smuggle new items or
        # recipe instructions into a delivery-removal request.
        def quantities(rows):
            totals={}
            for row in rows:
                key=(row.get('item'),cint(row.get('takeaway')),row.get('notes') or '')
                totals[key]=totals.get(key,0)+flt(row.get('qty'))
            return totals
        original=quantities(doc.get('items') or [])
        if any(qty>original.get(key,0) for key,qty in quantities(payload.get('items') or []).items()):
            frappe.throw(_('Floor staff can request item removals only. Make other bill changes at the POS'))
    comment=(data.get('delivery_comment') or '').strip()
    if not 3<=len(comment)<=1000:frappe.throw(_('A removal comment and photo are required for delivered items'))
    if cint(data.get('delivery_revision',-1))!=saved['revision']:frappe.throw(_('Delivery changed. Refresh the order and request removal again'))
    file=_photo(data.get('delivery_photo'),doc.name)
    current=groups(doc.get('items'),doc.order_type)
    affected=[dict(key=key,name=current.get(key,{}).get('name') or key,qty=qty) for key,qty in impacted.items()]
    proof=dict(revision=saved['revision'],comment=comment,photo=file.name,url=file.file_url,lines=affected)
    payload[EVIDENCE]=proof
    changes=[]
    for key in sorted(set(current)|set(proposed)):
        before=flt(current.get(key,{}).get('qty'));after=flt(proposed.get(key,{}).get('qty'))
        if before!=after:
            row=current.get(key) or proposed[key]
            changes.append(dict(name=row['name'],qtyStr=f'{before:g} → {after:g}',
                totalStr=f'{impacted[key]:g} delivered' if key in impacted else ''))
    details=dict(delivery_removal=True,delivery_comment=comment,delivery_photo=file.file_url,
        lines=changes,
        footer_label='Delivered items affected',footer_value=str(sum(impacted.values())))
    return payload,details


@frappe.whitelist(methods=['POST'])
def request_removal(order_name,key,qty,comment,photo,expected_revision):
    doc=_order(order_name)
    if doc.docstatus!=0 or doc.pos_invoice:frappe.throw(_('Removal requests are available only for open, unpaid bills'))
    current=groups(doc.get('items'),doc.order_type);qty=flt(qty)
    if key not in current or qty<=0 or not qty.is_integer() or qty>current[key]['qty']:frappe.throw(_('Choose a valid removal quantity'))
    remaining=qty;items=[]
    for row in doc.get('items') or []:
        keep=flt(row.qty)
        if _key(row.item,row.get('takeaway'))==key:
            take=min(keep,remaining);keep-=take;remaining-=take
        if keep>0:items.append(dict(item=row.item,qty=keep,takeaway=cint(row.get('takeaway')),notes=row.get('notes') or ''))
    if not _impact(state(doc.name,True),groups(items,doc.order_type)):
        frappe.throw(_('This quantity does not remove delivered food. Make the normal bill change at the POS'))
    from luuvcrm.api.approvals import create_approval
    old=frappe.local.form_dict
    frappe.local.form_dict=frappe._dict(approval_type='edit',pos_order=doc.name,title='Delivered item removal',
        subhead='Order '+doc.name,action='update_items',action_payload=frappe.as_json({'items':items}),
        delivery_comment=comment,delivery_photo=photo,delivery_revision=expected_revision)
    try:return create_approval()
    finally:frappe.local.form_dict=old


@contextmanager
def approved_change(approval):
    """Scope the exception to this manager decision and these exact quantities."""
    action=approval.action;payload=_json(approval.action_payload)
    if action not in ('update_items','void') or not approval.pos_order or not frappe.db.exists('POS Order',approval.pos_order):
        yield;return
    doc=frappe.get_doc('POS Order',approval.pos_order,for_update=True)
    if doc.docstatus!=0 or doc.pos_invoice:
        yield;return
    saved=state(doc.name,True);proposed=groups(payload.get('items') or [],doc.order_type) if action=='update_items' else {}
    impacted=_impact(saved,proposed)
    if not impacted:
        yield;return
    roles.require_manager();proof=payload.get(EVIDENCE) or {}
    if not proof or cint(proof.get('revision',-1))!=saved['revision']:
        frappe.throw(_('Delivery changed after this request. Create a new removal request with comment and photo'))
    _photo(proof.get('photo'),doc.name)
    if not proof.get('comment') or len((approval.get('manager_comment') or '').strip())<3:
        frappe.throw(_('The manager must add a comment before deciding this delivered-item removal'))
    previous=getattr(frappe.local,'delivery_approved_change',None)
    frappe.local.delivery_approved_change=dict(order=doc.name,counts={k:v['qty'] for k,v in proposed.items()})
    try:
        yield
        fresh=frappe.get_doc('POS Order',doc.name)
        actual={} if fresh.docstatus==2 else groups(fresh.get('items'),fresh.order_type)
        removed=_impact(saved,actual)
        if removed:
            for key,qty in removed.items():saved['delivered'][key]=max(0,flt(saved['delivered'][key])-qty)
            _save(fresh,saved,dict(kind='removed',approval=approval.name,lines=proof['lines'],
                comment=proof['comment'],manager_comment=approval.get('manager_comment'),photo=proof['url']))
    finally:frappe.local.delivery_approved_change=previous


def protect_photo(file,method=None):
    if file.attached_to_doctype!='POS Order':return
    for raw in frappe.get_all('POS Approval',filters={'pos_order':file.attached_to_name,
            'action_payload':['like','%'+file.name+'%']},pluck='action_payload'):
        if (_json(raw).get(EVIDENCE) or {}).get('photo')==file.name:
            frappe.throw(_('This photo is evidence for an approval and must be retained'))
