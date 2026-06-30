import sys, os
sys.path.insert(0, 'sites')
sys.path.insert(0, '.')
os.environ['HOME'] = '/home/frappe'
os.environ['USER'] = 'frappe'

import frappe
frappe.init('fryxo')
frappe.connect()
exec(open('/tmp/setup.py').read())
frappe.db.commit()
frappe.destroy()
print('Done')
