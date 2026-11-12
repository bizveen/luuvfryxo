"""Install only the additive branch category field and seed recorded choices."""
import sys
sys.path.insert(0,'/tmp/luuv-manager-history-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm.patches.v1_0.purchase_item_defaults import execute
    execute();frappe.db.commit()
    print('PASS branch category defaults installed; recorded corrections carried forward')
finally:frappe.db.rollback();frappe.destroy()
