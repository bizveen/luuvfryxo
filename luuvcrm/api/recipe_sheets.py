"""Manager-only branch recipe drafts; never used to consume stock."""
import frappe
from frappe import _
from frappe.utils import now_datetime
from luuvcrm import activity,recipe_sheets as sheets

FIELD='luuv_recipe_sheet_drafts'


def stored(warehouse,lock=False):
    if not frappe.db.has_column('Warehouse',FIELD):return {}
    if lock:
        rows=frappe.db.sql('SELECT '+FIELD+' FROM tabWarehouse WHERE name=%s FOR UPDATE',(warehouse,))
        raw=rows[0][0] if rows else None
    else:raw=frappe.db.get_value('Warehouse',warehouse,FIELD)
    return frappe.parse_json(raw or '{}')


def get(item,restaurant,warehouse):
    reference=sheets.references(restaurant).get(item)
    if not reference:return None
    return sheets.view(reference,stored(warehouse).get(item))


def save(item_code,ingredients,expected_revision,note='',restaurant=None):
    from luuvcrm.api.manager_inventory import _context,_check_item
    restaurant,warehouse=_context(restaurant)
    _check_item(item_code,restaurant)
    reference=sheets.references(restaurant).get(item_code)
    if not reference:frappe.throw(_('No supplied recipe draft was found for this item.'))
    if not frappe.db.has_column('Warehouse',FIELD):frappe.throw(_('Recipe draft storage needs setup.'))
    values=stored(warehouse,lock=True)
    history=values.get(item_code) or [sheets.initial(reference)]
    rows=frappe.parse_json(ingredients) if isinstance(ingredients,str) else ingredients
    try:revision=sheets.changed(history,rows,note,expected_revision,frappe.session.user,str(now_datetime()))
    except ValueError as e:frappe.throw(_(str(e)))
    if revision:
        values[item_code]=history+[revision]
        frappe.db.set_value('Warehouse',warehouse,FIELD,frappe.as_json(values))
        activity.log_activity('recipe.sheet',label='Recipe draft updated: '+reference['name'],
            ref_doctype='Item',ref_name=item_code,item=item_code,
            detail=dict(restaurant=restaurant,revision=revision['revision']))
    return sheets.view(reference,values.get(item_code) or history)
