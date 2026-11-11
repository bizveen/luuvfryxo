"""Native authenticated route rendering, schema and guest redirect checks."""
import base64
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch
from werkzeug.wrappers import Request

staged = '--staged' in sys.argv
if staged:
    sys.path.insert(0, '/tmp/luuv-pos-inventory-20261111/stage')
import frappe
frappe.init(site='fryxo')
frappe.connect()
try:
    from luuvcrm import branch
    from luuvcrm.api import pos_inventory as api
    from luuvcrm.www import pos, pos_inventory
    from luuvcrm.overrides.branding import update_website_context
    from frappe.website.path_resolver import resolve_path
    frappe.local.request = Request.from_values(base_url='https://luuvgrand.com', path='/pos-inventory')
    frappe.local.form_dict = frappe._dict()
    frappe.set_user('Administrator')
    scripts = {}
    with patch.object(branch, 'selected', return_value='Kurunegala'), \
         patch.object(frappe.sessions, 'get_csrf_token', return_value='release-render-check'):
        for route, module in [('pos', pos), ('pos-inventory', pos_inventory)]:
            expected = 'pos_inventory' if route == 'pos-inventory' else 'pos'
            if not staged:
                assert resolve_path(route) == expected
            context = frappe._dict()
            module.get_context(context)
            update_website_context(context)
            html = frappe.render_template(Path(frappe.get_app_path('luuvcrm', 'www', expected + '.html')).read_text(), context)
            assert 'const INVENTORY_POS = ' + ('true;' if route == 'pos-inventory' else 'false;') in html
            if route == 'pos-inventory':
                assert '>Inventory POS</div>' in html
            else:
                assert '>New order</div>' in html
            assert '{#' not in html
            scripts[route] = '\n'.join(re.findall(r'<script\b[^>]*>(.*?)</script>', html, re.S | re.I))
            print('PASS authenticated native render /' + route, flush=True)
        assert not frappe.db.count('Item', {'item_name': ['like', 'LFIP-%']})
        assert not frappe.db.count('POS Order', {'customer_name': ['like', 'LFIP-%']})
        assert frappe.db.count('POS Order', {api.FIELD: 1}) == 0
        assert frappe.db.count('BOM') == 0
        print('PASS no sample stock/orders/recipes left in production', flush=True)
    frappe.set_user('Guest')
    for module, route in [(pos, 'pos'), (pos_inventory, 'pos-inventory')]:
        try:
            module.get_context(frappe._dict())
        except frappe.Redirect:
            assert frappe.local.flags.redirect_location == '/pos-login?redirect=/' + route
        else:
            raise AssertionError('Guest reached ' + route)
    print('PASS correct login return for both POS routes', flush=True)
    print('SCRIPTS:' + base64.b64encode(json.dumps(scripts).encode()).decode(), flush=True)
finally:
    frappe.db.rollback()
    frappe.destroy()
