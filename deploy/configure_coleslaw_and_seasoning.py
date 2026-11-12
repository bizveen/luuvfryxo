"""Add a zero-stock coleslaw draft and 1 g seasoning per menu portion."""
from copy import deepcopy

COLESLAW='KG-STOCK-COLESLAW'
ITEMS=('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN','KG-FRIES-BEEF',
       'KG-FRIES-SAUSAGE','KG-CHIPS-FISH','KG-CHIPS-SPICY')
NOTE='Seasoning allocation: 1 g per finished menu portion.'


def seasoned(ingredients):
    rows=deepcopy(ingredients)
    matches=[r for r in rows if r['name'].strip().casefold() in ('seasoning','spicy seasoning')]
    if len(matches)>1:raise ValueError('Review multiple seasoning rows before updating.')
    if matches:matches[0].update(qty=1,unit='g')
    else:rows.append(dict(name='Seasoning',qty=1,unit='g'))
    return rows


def note_with_seasoning(note):
    if NOTE in note:return note
    value=(note+'\n'+NOTE).strip()
    if len(value)>1000:raise ValueError('Shorten the draft note before adding the seasoning allocation.')
    return value


def run_native():
    import frappe
    from unittest.mock import patch
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:
        from luuvcrm import branch,recipe_sheets
        from luuvcrm.api import manager_inventory as manager
        from luuvcrm.api.recipe_sheets import FIELD,stored
        with patch.object(branch,'selected',return_value='Kurunegala'):
            warehouse=branch.warehouse('Kurunegala')
            before=deepcopy(stored(warehouse,lock=True))
            if not frappe.db.exists('Item',COLESLAW):
                assert not frappe.db.exists('Item',{'item_name':'Coleslaw salad'}),'Review the existing coleslaw item before creating another.'
                frappe.get_doc(dict(doctype='Item',item_code=COLESLAW,item_name='Coleslaw salad',
                    stock_uom='Kg',item_group='Raw Materials',is_stock_item=1,is_purchase_item=0,is_sales_item=0,
                    description='Prepared in the branch; complete a preparation batch to add stock.')).insert(ignore_permissions=True)
            target=frappe.get_doc('Item',COLESLAW)
            assert target.is_stock_item and not target.disabled and target.stock_uom=='Kg'
            manager.create_prepared_recipe(COLESLAW,'Kurunegala')
            values=stored(warehouse,lock=True)
            values.setdefault(COLESLAW,[recipe_sheets.initial(recipe_sheets.references('Kurunegala')[COLESLAW])])
            frappe.db.set_value('Warehouse',warehouse,FIELD,frappe.as_json(values))
            updates=[]
            for code in ITEMS:
                history=before[code];latest=history[-1]
                saved=manager.save_sheet_draft(code,seasoned(latest['ingredients']),latest['revision'],note_with_seasoning(latest['note']),'Kurunegala')
                assert saved['history'][:len(history)]==history
                if len(saved['history'])>len(history):updates.append(code)
            for code,history in before.items():
                if code not in ITEMS:assert stored(warehouse)[code]==history
            salad=manager.recipe(COLESLAW)
            assert salad['kind']=='prepared' and salad['sheet_draft']['reference']['sample']
            for code in ITEMS:
                current=manager.recipe(code)['sheet_draft']['current']
                assert current['ingredients']==seasoned(current['ingredients'])
            frappe.db.commit()
            print(frappe.as_json(dict(coleslaw_recipe_visible=True,seasoning_recipes_updated=updates,
                sample_recipe_activated=False,stock_movements_posted=0,previous_history_preserved=True)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
