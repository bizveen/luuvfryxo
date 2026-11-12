"""Native rollback-only delivery/removal regression. Never run a general test suite on live."""
import base64,io,json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-waiter-delivery-20261112/stage')
import frappe,luuvcrm
assert '/tmp/luuv-waiter-delivery-20261112/stage/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
files=[];token='LFDELIVERY-'+frappe.generate_hash(length=10)
try:
    from luuvcrm import branch,activity,roles
    from luuvcrm.api import waiter_delivery as delivery,orders,approvals,payments,removal_report
    from luuvcrm.hooks import doc_events
    hooks=frappe.get_doc_hooks()
    for dt,events in doc_events.items():
        if dt in ('POS Order','File'):
            for event,handlers in events.items():hooks.setdefault(dt,{})[event]=handlers if isinstance(handlers,list) else [handlers]
    def call(fn,**data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    def blocked(fn,text):
        point='check_'+frappe.generate_hash(length=8);frappe.db.savepoint(point)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as error:
            assert text.lower() in str(error).lower(),str(error)
            frappe.db.rollback(save_point=point);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    def counts():return tuple(frappe.db.count(dt) for dt in ('Stock Entry','POS Invoice','Payment Entry'))
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        meal=token+'-MEAL'
        frappe.get_doc(dict(doctype='Item',item_code=meal,item_name='Sample loaded fries',item_group='Raw Materials',stock_uom='Nos',is_stock_item=0,is_sales_item=1,standard_rate=100)).insert(ignore_permissions=True)
        def new(qty=3):
            return call(orders.send_to_kitchen,items=json.dumps([dict(item=meal,qty=qty)]),customer_name=token,order_type='Take Away',order_source='Walk-in',draft=1)['name']
        def view(name):return delivery._view(frappe.get_doc('POS Order',name))
        def confirm(name,qty=1,revision=None,request=None):
            v=view(name)
            return delivery.confirm(name,[dict(key=v['lines'][0]['key'],qty=qty)],v['revision'] if revision is None else revision,request or frappe.generate_hash(length=32))
        order=new();other=new();plain=new();key=view(order)['lines'][0]['key'];baseline=counts()
        # Real floor and cashier users ensure this is more than an Administrator-only test.
        staff={}
        for tag,role in [('waiter',roles.WAITER),('cashier',roles.CASHIER),('manager',roles.RESTAURANT_MANAGER)]:
            email=(token+'-'+tag+'@example.invalid').lower()
            frappe.get_doc(dict(doctype='User',email=email,first_name='Delivery test '+tag,send_welcome_email=0,roles=[dict(role=role)])).insert(ignore_permissions=True)
            branch.assign(email,'Kurunegala');staff[tag]=email
        frappe.set_user(staff['waiter'])
        request=frappe.generate_hash(length=32);confirm(order,2,request=request)
        assert view(order)['lines'][0]['delivered']==2 and view(order)['lines'][0]['remaining']==1
        assert confirm(order,2,revision=0,request=request)['already_processed']
        assert view(order)['revision']==1
        blocked(lambda:confirm(order,2),'exceeds')
        blocked(lambda:confirm(order,1,revision=0),'changed')
        blocked(lambda:confirm(order,1,request=request),'already used')
        assert any(o['name']==order for o in delivery.orders()['orders'])
        assert counts()==baseline and frappe.db.get_value('POS Order',order,'docstatus')==0
        print('PASS waiter access, partial confirmation, idempotent retry, stale/excess quantities; no stock or payment changes',flush=True)
        frappe.set_user('Administrator')
        blocked(lambda:call(orders.update_order,order_name=order,items=json.dumps([dict(item=meal,qty=1,takeaway=1)])),'Delivered items are locked')
        blocked(lambda:call(orders.cancel_order,order_name=order),'Delivered items are locked')
        blocked(lambda:call(orders.delete_draft_order,order_name=order),'history must be retained')
        def clear_history():
            doc=frappe.get_doc('POS Order',order);doc.set(delivery.FIELD,'');doc.save(ignore_permissions=True)
        blocked(clear_history,'cannot be edited')
        call(orders.update_order,order_name=order,items=json.dumps([dict(item=meal,qty=2,takeaway=1)]))
        assert view(order)['lines'][0]['delivered']==2
        call(orders.update_order,order_name=plain,items=json.dumps([dict(item=meal,qty=1,takeaway=1)]))
        assert view(plain)['lines'][0]['qty']==1
        print('PASS direct POS edit/cancel/delete and history tampering blocked; unconfirmed quantity edits still work',flush=True)
        # Private photo upload uses native File storage; exact test files are removed after rollback.
        from PIL import Image
        raw=io.BytesIO();Image.new('RGB',(16,16),(int(token[-2:],16) if all(c in '0123456789abcdef' for c in token[-2:]) else 75,120,191)).save(raw,format='PNG')
        def upload(name):
            result=delivery.upload_photo(name,base64.b64encode(raw.getvalue()).decode());file=frappe.get_doc('File',result['photo']);files.append(Path(file.get_full_path()).resolve());return result['photo']
        photo=upload(order)
        blocked(lambda:delivery.upload_photo(order,base64.b64encode(b'not an image').decode()),'valid JPG')
        blocked(lambda:delivery._photo(photo,other),'this order')
        def request_removal(name=order,qty=1,comment='Wrong item delivered',proof=photo):
            v=view(name);return delivery.request_removal(name,v['lines'][0]['key'],qty,comment,proof,v['revision'])['name']
        frappe.set_user(staff['waiter'])
        blocked(lambda:request_removal(comment=''),'comment and photo')
        blocked(lambda:request_removal(proof=''),'Attach a photo')
        blocked(lambda:call(approvals.create_approval,approval_type='discount',title=token),'operate the POS')
        blocked(lambda:call(approvals.create_approval,approval_type='edit',pos_order=order,action='update_items',action_payload=json.dumps({'items':[dict(item=meal,qty=1,takeaway=0)]}),delivery_comment='Wrong item',delivery_photo=photo,delivery_revision=view(order)['revision']),'removals only')
        approval=request_removal()
        assert frappe.db.get_value('POS Approval',approval,'requested_by')==staff['waiter']
        assert request_removal()==approval
        assert view(order)['lines'][0]['qty']==2
        frappe.set_user(staff['cashier'])
        blocked(lambda:call(approvals.resolve_approval,name=approval,decision='approved',comment='Reviewed photo'),'Only a manager')
        blocked(lambda:call(approvals.resolve_approval,name=approval,decision='rejected',comment='Reviewed photo'),'Only a manager')
        frappe.set_user(staff['manager'])
        blocked(lambda:call(approvals.resolve_approval,name=approval,decision='approved'),'manager comment')
        call(approvals.resolve_approval,name=approval,decision='approved',comment='Photo checked, wrong item')
        assert view(order)['lines'][0]['qty']==1 and view(order)['lines'][0]['delivered']==1
        state=delivery.state(order);assert [e['kind'] for e in state['events']]==['confirmed','removed']
        assert state['events'][0]['lines'][0]['qty']==2 and state['events'][1]['by']==staff['manager']
        card=next(r for r in removal_report.history()['rows'] if r['name']==approval)
        assert card['delivery_removal'] and card['image'] and card['by']=='Delivery test waiter' and card['resolved_by']=='Delivery test manager'
        assert card['created_when'] and card['resolved_when'] and card['manager_comment']
        blocked(lambda:frappe.delete_doc('File',photo,ignore_permissions=True),'must be retained')
        print('PASS comment/photo validation, manager-only decisions, dedupe, approved quantity change and evidence/history',flush=True)
        # A new delivery invalidates approval evidence even when order quantities did not change.
        frappe.set_user('Administrator');late=new(3);latephoto=upload(late);confirm(late,2)
        lateapproval=request_removal(late,2,proof=latephoto);confirm(late,1)
        blocked(lambda:call(approvals.resolve_approval,name=lateapproval,decision='approved',comment='Reviewed'),'Delivery changed')
        assert frappe.db.get_value('POS Approval',lateapproval,'status')=='pending' and view(late)['lines'][0]['qty']==3
        fresh=request_removal(late,3,proof=latephoto)
        call(approvals.resolve_approval,name=fresh,decision='approved',comment='All items returned')
        assert frappe.db.get_value('POS Order',late,'docstatus')==2 and delivery.state(late)['delivered'][key]==0
        blocked(lambda:frappe.delete_doc('POS Order',late,ignore_permissions=True,force=True),'history must be retained')
        assert counts()==baseline
        print('PASS stale delivery proof rolls back decision; approved full removal voids only the open bill and retains history',flush=True)
        pre_delivery=new(2)
        old_request=call(approvals.create_approval,approval_type='edit',pos_order=pre_delivery,action='update_items',action_payload=json.dumps({'items':[dict(item=meal,qty=1,takeaway=1)]}))['name']
        confirm(pre_delivery,2)
        blocked(lambda:call(approvals.resolve_approval,name=old_request,decision='approved',comment='Reviewed'),'Delivery changed')
        assert frappe.db.get_value('POS Approval',old_request,'status')=='pending'
        # Confirm a genuinely paid invoice, without submitting/changing it again.
        with patch.object(payments,'_active_pos_profile_name',return_value='Kurunegala'),patch.object(payments,'_ensure_open_shift'):
            if not frappe.db.exists('POS Opening Entry',{'user':'Administrator','pos_profile':'Kurunegala','status':'Open','docstatus':1}):
                opening=frappe.get_doc(dict(doctype='POS Opening Entry',user='Administrator',pos_profile='Kurunegala',company='Luuv Fryxo',period_start_date=frappe.utils.now_datetime(),posting_date=frappe.utils.today(),balance_details=[dict(mode_of_payment='Cash',opening_amount=0)]));opening.insert(ignore_permissions=True);opening.submit()
            paid=new(1);call(payments.process_payment,order_name=paid,payment_mode='Cash')
        paid_before=frappe.get_doc('POS Order',paid)
        def invoice_snapshot():
            doc=frappe.get_doc('POS Invoice',paid_before.pos_invoice)
            return dict(header=frappe.db.get_value('POS Invoice',doc.name,'*',as_dict=True),
                children={field.fieldname:frappe.get_all(field.options,filters={'parent':doc.name,'parenttype':'POS Invoice'},fields=['*'],order_by='idx') for field in doc.meta.get_table_fields()})
        invoice_before=invoice_snapshot();counts_before=counts()
        frappe.set_user(staff['waiter']);confirm(paid)
        assert view(paid)['paid'] and view(paid)['complete']
        assert frappe.db.get_value('POS Order',paid,'docstatus')==paid_before.docstatus
        assert invoice_snapshot()==invoice_before
        assert counts()==counts_before,(counts(),counts_before)
        blocked(lambda:request_removal(paid),'open, unpaid')
        print('PASS real paid order delivery; invoice, payment, stock and bill status unchanged; paid removals denied',flush=True)
        frappe.set_user('Administrator')
        frappe.db.set_value('POS Order',paid,'docstatus',1)
        def edit_submitted_history():
            doc=frappe.get_doc('POS Order',paid);doc.set(delivery.FIELD,'');doc.save(ignore_permissions=True)
        blocked(edit_submitted_history,'cannot be edited')
        other_branch=next(r for r in branch.all_restaurants() if r!='Kurunegala')
        with patch.object(branch,'selected',return_value=other_branch):
            blocked(lambda:confirm(order),'another restaurant')
            assert order not in [o['name'] for o in delivery.orders()['orders']]
        frappe.set_user('Guest');blocked(lambda:delivery.orders(),'floor staff');frappe.set_user('Administrator')
        assert not getattr(frappe.local,'delivery_approved_change',None)
        print('PASS branch isolation, guest denial and authorization cleanup',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('POS Order',{'customer_name':token}) and not frappe.db.exists('Item',meal)
    assert all(not frappe.db.exists('User',u) for u in staff.values())
    print('PASS all native fixtures rolled back',flush=True)
finally:
    frappe.db.rollback()
    private=Path(frappe.get_site_path('private','files')).resolve()
    for path in set(files):
        assert path.parent==private and path.name.startswith('delivery-') and path.suffix=='.jpg'
        if not frappe.db.exists('File',{'file_url':'/private/files/'+path.name}) and path.exists():path.unlink()
    frappe.destroy()
