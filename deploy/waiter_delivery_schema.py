"""Install only the additive delivery history field; existing values are retained."""
import sys
sys.path.insert(0,'/tmp/luuv-waiter-delivery-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm.patches.v1_0.waiter_delivery import execute
    execute();frappe.db.commit()
    print('PASS additive delivery history field installed')
finally:frappe.db.rollback();frappe.destroy()
