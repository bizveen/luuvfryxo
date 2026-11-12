"""Branch purchase defaults; changing a default never rewrites previous bills."""
import frappe
from frappe import _
from luuvcrm import branch

FIELD='luuv_purchase_categories'

def key(name):
    return ' '.join((name or '').split()).casefold()

def read(restaurant=None):
    warehouse=branch.warehouse(restaurant or branch.viewing())
    raw=frappe.db.get_value('Warehouse',warehouse,FIELD) if warehouse and frappe.db.has_column('Warehouse',FIELD) else None
    return frappe.parse_json(raw) if raw else {'items':{},'names':{}}

def remember(doc,changes,actor=None,when=None,fill_only=False):
    if not changes:return
    warehouse=branch.warehouse(doc.get(branch.FIELD) or branch.viewing())
    if not warehouse or not frappe.db.has_column('Warehouse',FIELD):
        frappe.throw(_('Purchase category defaults are not configured for this branch'))
    frappe.db.sql('SELECT name FROM tabWarehouse WHERE name=%s FOR UPDATE',(warehouse,))
    data=read(doc.get(branch.FIELD))
    for change in changes:
        category=change.get('after') or change.get('to')
        if not category:continue
        value=dict(category=category,name=change.get('name') or '',item=change.get('item') or '',
                   by=actor or frappe.session.user,when=str(when or frappe.utils.now_datetime()),bill=doc.name)
        if value['item'] and (not fill_only or value['item'] not in data.get('items',{})):
            data.setdefault('items',{})[value['item']]=value
        if key(value['name']) and (not fill_only or key(value['name']) not in data.get('names',{})):
            data.setdefault('names',{})[key(value['name'])]=value
    frappe.db.set_value('Warehouse',warehouse,FIELD,frappe.as_json(data),update_modified=False)

def seed_history():
    """Carry existing explicit corrections forward; newest recorded choice wins."""
    if not frappe.db.exists('DocType','Purchase Bill'):return
    for version in frappe.get_all('Version',filters={'ref_doctype':'Purchase Bill',
            'data':['like','%purchase_category_%']},fields=['docname','data','owner','creation'],
            order_by='creation desc, name desc'):
        if not frappe.db.exists('Purchase Bill',version.docname):continue
        data=frappe.parse_json(version.data)
        record=data.get('purchase_category_correction') or data.get('purchase_category_change') or {}
        changes=record.get('changes') or []
        doc=frappe.get_doc('Purchase Bill',version.docname)
        if not branch.warehouse(doc.get(branch.FIELD)):continue
        rows={r.name:r for r in doc.get('items') or []}
        for change in changes:
            row=rows.get(change.get('row_name'))
            if row:
                change.setdefault('item',row.item);change.setdefault('name',row.item_name or row.item)
        remember(doc,changes,actor=version.owner,when=version.creation,fill_only=True)

def category(data,item=None,name=None):
    value=data.get('items',{}).get(item) or data.get('names',{}).get(key(name)) or {}
    chosen=value.get('category')
    if chosen and frappe.db.exists('Expense Category',{'name':chosen,'disabled':0}):return chosen
    return None

@frappe.whitelist()
def catalog():
    """Purchase 2.0 sees saved choices, including descriptive items without an Item ID."""
    from luuvcrm.api.purchases import get_purchase_catalog
    from luuvcrm.api.expenses import category_has_item_list
    result=get_purchase_catalog()  # Enforces purchase access.
    data=read();known=set()
    for row in result['items']:
        chosen=category(data,row['item'],row['name'])
        row['default_category']=chosen or ''
        known.add(key(row['name']))
    for label,value in data.get('names',{}).items():
        chosen=category(data,name=label)
        if label in known or not chosen or category_has_item_list(chosen):continue
        result['items'].append(dict(item='saved:'+label,name=value['name'],unit='Bill',price=0,
            group='Saved purchases',default_category=chosen,saved_description=True,is_fixed_asset=0,asset_category=''))
    return result
