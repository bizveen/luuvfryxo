"""Branch preparation: consume immediate ingredients, receive finished stock once."""
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import flt,now_datetime
from luuvcrm import activity,branch

REGISTRY='luuv_prepared_items'
SNAPSHOT='luuv_preparation_details'
REQUEST='luuv_preparation_request'


def registered(warehouse,lock=False):
    if not frappe.db.has_column('Warehouse',REGISTRY):return []
    if lock:
        rows=frappe.db.sql('SELECT '+REGISTRY+' FROM tabWarehouse WHERE name=%s FOR UPDATE',(warehouse,))
        raw=rows[0][0] if rows else None
    else:raw=frappe.db.get_value('Warehouse',warehouse,REGISTRY)
    return frappe.parse_json(raw or '[]')


def codes(restaurant,warehouse):
    from luuvcrm.api.manager_inventory import _menu
    menu=set(_menu(restaurant))
    managed=frappe.get_all('BOM',filters={'luuv_recipe_restaurant':restaurant,'docstatus':['<',2]},pluck='item',limit_page_length=0)
    return (set(registered(warehouse))|set(managed))-menu


def check(item,restaurant,warehouse):
    from luuvcrm.api.manager_inventory import _catalog
    if item not in codes(restaurant,warehouse) or item not in {r['item'] for r in _catalog(warehouse)}:
        frappe.throw(_('Choose a prepared stock item in this branch.'),frappe.PermissionError)


def create(item,restaurant=None):
    from luuvcrm.api.manager_inventory import _context,_catalog,_menu,recipe
    restaurant,warehouse=_context(restaurant)
    values=registered(warehouse,lock=True)
    if item in _menu(restaurant) or item not in {r['item'] for r in _catalog(warehouse)}:
        frappe.throw(_('Choose the finished stock item first.'))
    if item not in values:
        values.append(item)
        frappe.db.set_value('Warehouse',warehouse,REGISTRY,frappe.as_json(values))
        activity.log_activity('recipe.prepared',label='Prepared item registered: '+item,
            ref_doctype='Item',ref_name=item,item=item,detail={'restaurant':restaurant})
    return recipe(item,restaurant)


def cycle_check(item,ingredients,restaurant,warehouse):
    graph={}
    for code in sorted(codes(restaurant,warehouse)):
        versions=frappe.get_all('BOM',filters={'item':code,'luuv_recipe_restaurant':restaurant,'docstatus':['<',2]},
            fields=['name','docstatus'],order_by='creation desc',limit_page_length=0)
        # Include the latest draft and the active version: either may be used next.
        selected=versions[:1]+next(([v] for v in versions if v.docstatus==1),[])
        graph[code]=set(frappe.get_all('BOM Item',filters={'parent':['in',[v.name for v in selected]]},pluck='item_code')) if selected else set()
    graph[item]=set(ingredients)
    def visit(node,path):
        if node in path:frappe.throw(_('A prepared recipe cannot contain itself, directly or through another recipe.'))
        for child in graph.get(node,()):visit(child,path|{node})
    visit(item,set())


def batches(item,warehouse):
    if not frappe.db.has_column('Stock Entry',SNAPSHOT):return []
    rows=frappe.get_all('Stock Entry',filters={'to_warehouse':warehouse,'docstatus':1,SNAPSHOT:['is','set']},
        fields=['name','creation',SNAPSHOT],order_by='creation desc',limit_page_length=0)
    result=[]
    for row in rows:
        details=frappe.parse_json(row[SNAPSHOT] or '{}')
        if details.get('item')==item:result.append(dict(name=row.name,creation=row.creation,details=details))
        if len(result)==25:break
    return result


def whole(item,qty):
    unit=frappe.db.get_value('Item',item,'stock_uom')
    if frappe.db.get_value('UOM',unit,'must_be_whole_number') and abs(qty-round(qty))>1e-8:
        frappe.throw(_('Enter a whole number of {0} for {1}.').format(unit,item))


def complete(item_code,version,request_id,planned_qty,actual_qty,comment='',restaurant=None):
    from luuvcrm.api.manager_inventory import _context,_number,_display
    from luuvcrm.api.inventory import _submit
    restaurant,warehouse=_context(restaurant)
    request_id=(request_id or '').strip()
    if not 8<=len(request_id)<=100:frappe.throw(_('Reopen the batch form before completing it.'))
    planned=_number(planned_qty,'Recipe quantity');actual=_number(actual_qty,'Finished quantity')
    comment=(comment or '').strip()
    if len(comment)>1000:frappe.throw(_('Keep the batch note under 1000 characters.'))
    if planned!=actual and len(comment)<3:frappe.throw(_('Add a note for the difference in finished quantity.'))
    key=hashlib.sha256((warehouse+'|'+request_id).encode()).hexdigest()
    payload_hash=hashlib.sha256(json.dumps([item_code,version,planned,actual,comment]).encode()).hexdigest()
    # Serialize retries before checking the unique request key. The whole request
    # stays in Frappe's transaction; failures roll back both stock and its receipt.
    registered(warehouse,lock=True)
    existing=frappe.db.get_value('Stock Entry',{REQUEST:key},['name','docstatus',SNAPSHOT],as_dict=True)
    if existing:
        saved=frappe.parse_json(existing[SNAPSHOT] or '{}')
        if existing.docstatus!=1 or saved.get('payload_hash')!=payload_hash:
            frappe.throw(_('This batch was already completed with different values or cancelled. Reopen the form.'))
        return dict(saved,name=existing.name,already_processed=True)
    check(item_code,restaurant,warehouse)
    frappe.db.sql('SELECT name FROM tabItem WHERE name=%s FOR UPDATE',(item_code,))
    active=frappe.get_all('BOM',filters={'item':item_code,'luuv_recipe_restaurant':restaurant,'docstatus':1},
        pluck='name',order_by='luuv_recipe_effective_from desc',limit_page_length=1)
    if not active or active[0]!=version:frappe.throw(_('The active preparation recipe changed. Reopen the batch form.'))
    bom=frappe.get_doc('BOM',version)
    output=frappe.get_doc('Item',item_code)
    output_uom,factor=_display(output.stock_uom)
    received=round(actual/factor,6);planned_stock=planned/factor
    if received<=0:frappe.throw(_('Finished quantity is too small for the stock unit.'))
    whole(item_code,received);whole(item_code,planned_stock)
    from erpnext.stock.utils import get_or_make_bin
    balances={}
    for code in sorted({item_code}|{r.item_code for r in bom.items}):
        name=get_or_make_bin(code,warehouse)
        balances[code]=frappe.db.sql('SELECT actual_qty,valuation_rate FROM tabBin WHERE name=%s FOR UPDATE',(name,),as_dict=True)[0]
    used=[];rows=[];cost=0
    for line in bom.items:
        item=frappe.get_doc('Item',line.item_code)
        if item.disabled or not item.is_stock_item:frappe.throw(_('Choose enabled stock ingredients.'))
        quantity=round(flt(line.stock_qty)*planned_stock/flt(bom.quantity),6)
        if quantity<=0:frappe.throw(_('Batch quantity is too small for {0}.').format(item.item_name))
        whole(item.name,quantity)
        before=flt(balances[item.name].actual_qty)
        if before+1e-8<quantity:frappe.throw(_('Insufficient stock: {0}').format(item.item_name))
        rate=flt(balances[item.name].valuation_rate);cost+=quantity*rate
        used.append(dict(item=item.name,name=item.item_name,qty=quantity,uom=item.stock_uom,before=before,after=before-quantity))
        rows.append(dict(item_code=item.name,qty=quantity,uom=item.stock_uom,stock_uom=item.stock_uom,
            conversion_factor=1,s_warehouse=warehouse,basic_rate=rate,allow_zero_valuation_rate=int(not rate),
            cost_center=branch.cost_center(restaurant)))
    before=flt(balances[item_code].actual_qty)
    details=dict(item=item_code,item_name=output.item_name,recipe=version,recipe_note=bom.get('luuv_preparation_note') or '',
        recipe_output_qty=flt(bom.quantity)*factor,planned_qty=planned,actual_qty=actual,
        output_uom=output_uom,received_qty=received,stock_uom=output.stock_uom,before=before,after=before+received,
        ingredients=used,comment=comment,cost=cost,owner=frappe.session.user,restaurant=restaurant,payload_hash=payload_hash)
    rows.append(dict(item_code=item_code,qty=received,uom=output.stock_uom,stock_uom=output.stock_uom,
        conversion_factor=1,t_warehouse=warehouse,is_finished_item=1,basic_rate=cost/received,
        allow_zero_valuation_rate=int(not cost),cost_center=branch.cost_center(restaurant)))
    entry=frappe.get_doc(dict(doctype='Stock Entry',stock_entry_type='Manufacture',purpose='Manufacture',
        company=frappe.db.get_value('Warehouse',warehouse,'company'),from_warehouse=warehouse,to_warehouse=warehouse,
        bom_no=version,fg_completed_qty=received,use_multi_level_bom=0,items=rows,
        remarks='Prepared batch: '+output.item_name+(' · '+comment if comment else ''),
        **{REQUEST:key,SNAPSHOT:frappe.as_json(details)}))
    entry=_submit(entry)
    activity.log_activity('stock.prepare',label='Prepared '+str(actual)+' '+output_uom+' '+output.item_name,
        ref_doctype='Stock Entry',ref_name=entry.name,item=item_code,qty=received,detail={'restaurant':restaurant,'recipe':version})
    return dict(details,name=entry.name)
