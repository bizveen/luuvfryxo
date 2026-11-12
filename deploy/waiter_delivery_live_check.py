"""Read-only delivery route, permission, branch and service smoke checks."""
import sys,time
from unittest.mock import patch
from werkzeug.wrappers import Request
if '--staged' in sys.argv:sys.path.insert(0,'/tmp/luuv-waiter-delivery-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import waiter_delivery,approvals
    from luuvcrm.www import waiter_v2
    from frappe.website.path_resolver import resolve_path
    frappe.local.request=Request.from_values(base_url='https://luuvgrand.com',path='/waiter2.0')
    frappe.local.form_dict=frappe._dict()
    assert frappe.db.has_column('POS Order',waiter_delivery.FIELD)
    if '--staged' not in sys.argv:
        assert resolve_path('waiter2.0')=='waiter_v2'
        hooks=frappe.get_doc_hooks()['POS Order']
        assert 'luuvcrm.api.waiter_delivery.guard' in hooks['validate']
        assert 'luuvcrm.api.waiter_delivery.guard' in hooks['before_update_after_submit']
    with patch.object(branch,'selected',return_value='Kurunegala'):
        start=time.monotonic();data=waiter_delivery.orders();elapsed=time.monotonic()-start
        assert data['restaurant']=='Kurunegala'
        print('PASS delivery list:',len(data['orders']),'orders,',round(elapsed,2),'seconds')
        assert elapsed<5,'Delivery refresh too slow'
        approvals.get_pending_approvals()
        print('PASS existing manager approvals readable')
    frappe.set_user('Guest')
    try:waiter_delivery.orders()
    except frappe.PermissionError:print('PASS guest API denied')
    else:raise AssertionError('Guest access')
    try:waiter_v2.get_context(frappe._dict())
    except frappe.Redirect:
        assert frappe.local.flags.redirect_location=='/pos-login?redirect=/waiter2.0'
        print('PASS login returns to Waiter 2.0')
    else:raise AssertionError('Guest page redirect')
    frappe.set_user('Administrator')
    assert not frappe.db.exists('POS Order',{'customer_name':['like','LFDELIVERY-%']})
    assert not frappe.db.exists('User',{'email':['like','lfdelivery-%@example.invalid']})
    print('PASS temporary test records absent')
finally:frappe.db.rollback();frappe.destroy()
