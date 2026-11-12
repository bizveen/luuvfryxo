"""Read only: inspect installed unit validation and unit metadata."""
import inspect
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from erpnext.stock.doctype.item.item import Item
    for method in ('validate_stock_uom','validate_uom','validate_uom_conversion_factor'):
        if hasattr(Item,method):print(inspect.getsource(getattr(Item,method)))
    print('UOM fields:',[(f.fieldname,f.fieldtype) for f in frappe.get_meta('UOM').fields])
    print('Item unit fields:',[(f.fieldname,f.fieldtype) for f in frappe.get_meta('Item').fields if 'uom' in f.fieldname])
    print('Common defined units:',frappe.get_all('UOM',filters={'name':['in',['Kg','g','L','Litre','ml','Nos','Pkt','Box','Pack','Dozen']]},fields=['name','must_be_whole_number']))
    print('Stock ledger unit fields:',[f.fieldname for f in frappe.get_meta('Stock Ledger Entry').fields if 'uom' in f.fieldname])
finally:frappe.db.rollback();frappe.destroy()
