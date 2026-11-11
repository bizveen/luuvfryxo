"""Keep the original UI intact while serving version 2 with the same role gates."""
import hashlib
import importlib.util
from http.server import ThreadingHTTPServer
from pathlib import Path
import runpy
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from server import Store, handler

ROOT = Path(__file__).resolve().parents[1]


class VersionedRoutesTest(unittest.TestCase):
    def test_legacy_templates_match_verified_live_versions(self):
        for name, expected in {
            'manager': '1cc4cc8bc45ddf33d25a4e5ed812bd078a8b35f57927e92c2e78a517d7bfa6e1',
            'purchases': 'cf5d1947361006446aa2d30a80fcbb7a0b355ce78aa715bf810b984ebf12871b',
        }.items():
            with self.subTest(page=name):
                self.assertEqual(hashlib.sha256((ROOT/'luuvcrm/www'/f'{name}.html').read_bytes()).hexdigest(), expected)

    def test_public_aliases_resolve_to_importable_controllers(self):
        rules = runpy.run_path(str(ROOT/'luuvcrm/hooks.py'))['website_route_rules']
        self.assertEqual({r['from_route']: r['to_route'] for r in rules},
                         {'/manager2.0': 'manager_v2', '/purchase2.0': 'purchase_v2'})
        for rule in rules:
            for ext in ('html', 'py'):
                self.assertTrue((ROOT/'luuvcrm/www'/f"{rule['to_route']}.{ext}").is_file())

    def test_versioned_pages_reuse_roles_and_keep_login_return_routes(self):
        class Redirect(Exception):
            pass
        for module, base, route in [('manager_v2','manager','manager2.0'),
                                     ('purchase_v2','purchases','purchase2.0')]:
            with self.subTest(page=module):
                context = SimpleNamespace()
                original = Mock(return_value=context)
                frappe = SimpleNamespace(session=SimpleNamespace(user='Guest'),
                    local=SimpleNamespace(flags=SimpleNamespace()), Redirect=Redirect)
                with patch.dict(sys.modules, {'frappe': frappe,
                        'luuvcrm.www.'+base: SimpleNamespace(get_context=original)}):
                    spec=importlib.util.spec_from_file_location('route_test_'+module,ROOT/'luuvcrm/www'/f'{module}.py')
                    page=importlib.util.module_from_spec(spec);spec.loader.exec_module(page)
                    with self.assertRaises(Redirect): page.get_context(context)
                    self.assertEqual(frappe.local.flags.redirect_location,'/pos-login?redirect=/'+route)
                    original.assert_not_called()
                    frappe.session.user='staff@example.test'
                    self.assertIs(page.get_context(context),context)
                    original.assert_called_once_with(context)
                    original.side_effect=PermissionError('Role required')
                    with self.assertRaises(PermissionError): page.get_context(context)

    def test_http_serves_distinct_screens_and_versioned_cross_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=ThreadingHTTPServer(('127.0.0.1',0),handler(Store(Path(tmp)/'review.sqlite3')))
            thread=Thread(target=app.serve_forever,daemon=True);thread.start()
            try:
                pages={}
                for route in ('/manager','/purchases','/manager2.0','/purchase2.0','/pos'):
                    with urlopen(f'http://127.0.0.1:{app.server_port}'+route) as response:
                        pages[route]=response.read().decode()
                        if route!='/pos':
                            role='manager' if route.startswith('/manager') else 'purchaser'
                            self.assertIn('preview_role='+role,response.headers['Set-Cookie'])
                self.assertNotIn('id="tabInventory"',pages['/manager'])
                self.assertNotIn('window.LFPurchaseStock',pages['/purchases'])
                self.assertIn('id="tabInventory"',pages['/manager2.0'])
                self.assertIn('window.LFPurchaseStock',pages['/purchase2.0'])
                self.assertIn("const purchaseUrl='/purchase2.0?inventory=1&restaurant='",pages['/manager2.0'])
                self.assertIn('href="/manager2.0?tab=inventory"',pages['/purchase2.0'])
                self.assertIn("const DRAFT_KEY='lf_purch_draft'",pages['/purchases'])
                self.assertIn("const DRAFT_KEY='lf_purch_v2_draft'",pages['/purchase2.0'])
                self.assertIn('href="/manager2.0"',pages['/pos'])
                for route in ('/manager2.0','/purchase2.0'):
                    self.assertNotIn('navigator.serviceWorker.register',pages[route])
                    self.assertNotIn('rel="manifest"',pages[route])
            finally:
                app.shutdown();app.server_close();thread.join()


if __name__ == '__main__': unittest.main()
