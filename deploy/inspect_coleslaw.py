"""Read only the inventory names/units relevant to coleslaw preparation."""
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import manager_inventory as manager
    warehouse=branch.warehouse('Kurunegala')
    terms=['%coleslaw%','%salad%','%cabbage%','%carrot%','%onion%','%mayonnaise%','%seasoning%']
    matches={}
    for term in terms:
        for row in frappe.get_all('Item',or_filters={'name':['like',term],'item_name':['like',term]},
            fields=['name','item_name','stock_uom','is_stock_item','disabled'],limit_page_length=30):
            matches[row.name]=row
    print(frappe.as_json({'relevant_items':list(matches.values()),
        'prepared_api_present':hasattr(manager,'complete_batch'),
        'branch_prepared_boms':frappe.db.count('BOM',{'luuv_recipe_restaurant':'Kurunegala','item':['in',list(matches) or ['none']]}),
        'prepared_registry_present':frappe.db.has_column('Warehouse','luuv_prepared_items')}))
finally:frappe.db.rollback();frappe.destroy()
