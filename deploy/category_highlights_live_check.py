"""Read-only production smoke check for category markers and the requested category."""
import time
from unittest.mock import patch
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import purchases,expenses
    with patch.object(branch,'selected',return_value='Kurunegala'):
        started=time.monotonic()
        bills=purchases.get_purchase_bills()['bills']
        assert all('category_changed' in b for b in bills)
        assert any(c['name']=='Not sure' for c in expenses.get_expense_categories()['category_details'])
        print('PASS live bills:',len(bills),'highlighted:',sum(b['category_changed'] for b in bills),
              'seconds:',round(time.monotonic()-started,2),'Not sure available')
finally:frappe.db.rollback();frappe.destroy()
