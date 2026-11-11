"""Read-only release prerequisites; run in the live bench Python environment."""
import json
import frappe

frappe.init(site='fryxo')
frappe.connect()
try:
    from luuvcrm.api.menu import active_menu_name
    warehouse=frappe.db.get_value('Restaurant','Kurunegala','warehouse')
    fields={dt:{f:bool(frappe.db.has_column(dt,f)) for f in names} for dt,names in {
        'Restaurant':['luuv_pos_stock_policy'],
        'Warehouse':['luuv_stock_minimums'],
        'BOM':['luuv_recipe_restaurant','luuv_recipe_effective_from'],
        'Stock Entry':['luuv_pos_order','luuv_purchase_bill','luuv_stock_policy_snapshot'],
        'Stock Reconciliation':['luuv_audit_restaurant','luuv_audit_details'],
    }.items()}
    print(json.dumps(dict(warehouse=warehouse,menu=active_menu_name('Kurunegala'),fields=fields,
        raw_material_group=bool(frappe.db.exists('Item Group','Raw Materials')),
        bom_count=frappe.db.count('BOM'),stock_bins=frappe.db.count('Bin',{'warehouse':warehouse}),
        stock_settings=frappe.db.get_single_value('Stock Settings','allow_negative_stock'),
        latest_order_creation=frappe.db.sql('select max(creation) from `tabPOS Order`')[0][0]),default=str))
finally:
    frappe.db.rollback()
    frappe.destroy()
