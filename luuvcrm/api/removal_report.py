"""Read-only branch approval history, with a separate item-removal report."""
import json
import math
from collections import Counter
from datetime import timedelta
import frappe
from frappe import _
from luuvcrm import branch,roles

def _json(raw):
    try:
        result=json.loads(raw or '{}')
        return result if isinstance(result,dict) else {}
    except (TypeError,ValueError):return {}

def is_removal(row):
    if row.get('action')!='update_items':return False
    payload=_json(row.get('action_payload'))
    base=payload.get('_approval_base') or {}
    if isinstance(base,dict) and isinstance(base.get('items'),list):
        before=Counter();after=Counter()
        for item in base['items']:
            if isinstance(item,list) and len(item)>1:before[str(item[0])]+=_qty(item[1])
        for item in payload.get('items') or []:
            if isinstance(item,dict):after[str(item.get('item'))]+=_qty(item.get('qty'))
        return any(qty>after[code] for code,qty in before.items())
    details=_json(row.get('details_json'))
    return 'remov' in (row.get('title') or '').lower() or 'removed' in str(details.get('footer_label','')).lower()

def _qty(value):
    try:
        number=float(value or 0)
        return number if math.isfinite(number) else 0
    except (TypeError,ValueError):return 0

@frappe.whitelist()
def history(start=None,end=None,status='all',kind='removals',page=0):
    roles.require_manager()
    end=frappe.utils.getdate(end or frappe.utils.today())
    start=frappe.utils.getdate(start or end-timedelta(days=29))
    if start>end or (end-start).days>365:frappe.throw(_('Choose a date range of up to one year'))
    if status not in ('all','pending','approved','rejected','superseded') or kind not in ('all','removals','bill_changes'):
        frappe.throw(_('Choose a valid history filter'))
    page=max(0,frappe.utils.cint(page));size=50
    fields=['name','approval_type','pos_order','title','subhead','details_json','flag_text','status',
            'requested_by','requested_at','resolved_by','resolved_at','action','action_payload']
    if frappe.db.has_column('POS Approval','manager_comment'):fields.append('manager_comment')
    filters={branch.FIELD:branch.viewing(),'requested_at':['between',[str(start),str(end)+' 23:59:59.999999']]}
    if kind=='removals':filters['action']='update_items'
    if kind=='bill_changes':filters['pos_order']=['is','set']
    rows=frappe.get_all('POS Approval',filters=filters,fields=fields,
                        order_by='requested_at desc, name desc',limit_page_length=0)
    if kind=='removals':rows=[row for row in rows if is_removal(row)]
    counts=dict(Counter(row.status for row in rows));counts['all']=len(rows)
    if status!='all':rows=[row for row in rows if row.status==status]
    from luuvcrm.api.approvals import _format_approvals
    return dict(rows=_format_approvals(rows[page*size:(page+1)*size]),counts=counts,total=len(rows),
                page=page,page_size=size,start=str(start),end=str(end),kind=kind,status=status)
