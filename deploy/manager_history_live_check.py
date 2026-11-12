"""Read-only production/staged smoke check of approval reports and saved categories."""
import sys,time
from unittest.mock import patch
if '--staged' in sys.argv:sys.path.insert(0,'/tmp/luuv-manager-history-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import removal_report,purchase_item_categories,approvals
    with patch.object(branch,'selected',return_value='Kurunegala'):
        for kind in ('removals','bill_changes','all'):
            started=time.monotonic();result=removal_report.history(kind=kind)
            assert all('by' in r and 'resolved_by' in r and 'created_when' in r for r in result['rows'])
            print('PASS',kind,'requests',result['total'],'returned',len(result['rows']),'seconds',round(time.monotonic()-started,2))
        items=purchase_item_categories.catalog()['items']
        print('PASS catalog saved categories',sum(bool(r.get('default_category')) for r in items))
        assert not frappe.db.exists('POS Approval',{'title':['like','LFHISTORY-%']})
        assert not frappe.db.exists('Purchase Bill',{'vendor':['like','LFHISTORY-%']})
        print('PASS no temporary test records persisted')
finally:frappe.db.rollback();frappe.destroy()
