"""Install additive preparation fields from the reviewed staged release."""
import sys
sys.path.insert(0,'/tmp/luuv-coleslaw-preparation-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm.patches.v1_0.manager_preparation import execute
    execute()
    frappe.db.commit()
    print('Preparation registry, recipe notes and retry-safe batch receipt fields installed.')
finally:frappe.db.rollback();frappe.destroy()
