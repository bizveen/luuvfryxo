"""Install only the additive fields required by the version 2 UI."""
import sys
sys.path.insert(0,'/tmp/luuv-manager-v2-20261111/stage')
import frappe
import luuvcrm

assert '/tmp/luuv-manager-v2-20261111/stage/' in luuvcrm.__file__
frappe.init(site='fryxo')
frappe.connect()
try:
    frappe.set_user('Administrator')
    from luuvcrm.patches.v1_0.manager_inventory_versions import execute
    execute()
    frappe.db.commit()
    print('Installed 7 additive fields; no recipes, quantities or stock rules activated.')
finally:
    frappe.destroy()
