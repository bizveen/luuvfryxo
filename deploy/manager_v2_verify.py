"""Verify deployed routing, native authenticated rendering and clean live data."""
from pathlib import Path
from unittest.mock import patch
from werkzeug.wrappers import Request
import frappe

frappe.init(site='fryxo')
frappe.connect()
try:
    from luuvcrm import branch
    from luuvcrm.api import manager_inventory, inventory_purchasing
    from luuvcrm.www import manager_v2, purchase_v2, manager, purchases
    from luuvcrm.overrides.branding import update_website_context
    from frappe.website.path_resolver import resolve_path
    frappe.local.request=Request.from_values(base_url='https://luuvgrand.com',path='/manager2.0')
    frappe.local.form_dict=frappe._dict()
    frappe.set_user('Administrator')
    with patch.object(branch,'selected',return_value='Kurunegala'), \
         patch.object(frappe.sessions,'get_csrf_token',return_value='release-render-check'):
        for route,module in [('manager2.0',manager_v2),('purchase2.0',purchase_v2),
                             ('manager',manager),('purchases',purchases)]:
            endpoint=resolve_path(route)
            expected={'manager2.0':'manager_v2','purchase2.0':'purchase_v2'}.get(route,route)
            assert endpoint==expected,(route,endpoint)
            context=frappe._dict()
            module.get_context(context)
            update_website_context(context)
            html=frappe.render_template(Path(frappe.get_app_path('luuvcrm','www',endpoint+'.html')).read_text(),context)
            if route=='manager2.0':assert 'id="tabInventory"' in html and '/purchase2.0?inventory=1' in html
            if route=='purchase2.0':assert 'window.LFPurchaseStock' in html and '/manager2.0?tab=inventory' in html
            if route=='manager':assert 'id="tabInventory"' not in html
            if route=='purchases':assert 'window.LFPurchaseStock' not in html
            print('PASS authenticated native render /'+route,flush=True)
        data=manager_inventory.dashboard('Kurunegala')
        assert inventory_purchasing.restock('Kurunegala')['warehouse']==data['warehouse']
        assert data['stock_policy']['revision']==0
        assert frappe.db.count('Item',{'item_name':['like','LFV2-%']})==0
        assert frappe.db.count('BOM',{'item':['like','LFV2-%']})==0
        print('PASS live branch API queries; no test items/BOMs or activated rules',flush=True)
    frappe.set_user('Guest')
    for module,route in [(manager_v2,'manager2.0'),(purchase_v2,'purchase2.0')]:
        try:module.get_context(frappe._dict())
        except frappe.Redirect:
            assert frappe.local.flags.redirect_location=='/pos-login?redirect=/'+route
        else:raise AssertionError('Guest was allowed into '+route)
    print('PASS original authentication and versioned login returns',flush=True)
finally:
    frappe.db.rollback()
    frappe.destroy()
