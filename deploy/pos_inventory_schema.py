"""Add the mode field with a zero default; existing tickets stay ordinary."""
import sys
sys.path.insert(0, '/tmp/luuv-pos-inventory-20261111/stage')
import frappe
frappe.init(site='fryxo')
frappe.connect()
frappe.set_user('Administrator')
try:
    from luuvcrm.patches.v1_0.pos_inventory_mode import execute
    execute()
    frappe.db.commit()
    assert frappe.db.has_column('POS Order', 'luuv_inventory_pos')
    assert not frappe.db.count('POS Order', {'luuv_inventory_pos': 1})
    print('PASS additive schema; all existing orders remain ordinary POS orders', flush=True)
finally:
    frappe.destroy()
