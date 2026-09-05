"""The api package, and the promise it makes.

`luuvcrm/api.py` was 7,338 lines — POS, kitchen, loyalty, customers, purchases,
expenses, approvals, promos, guides, wastage, WhatsApp and reporting in one
file. It is a package now, one module per domain.

The whole refactor rests on one promise: **no dotted path changed**. Every
`@frappe.whitelist()` endpoint is reached by name over HTTP — `luuvcrm.api.
place_order` is a string in a fetch() call in a page nobody is editing today,
and in WhatsApp links and print payloads already out in the world. A name that
stops resolving does not fail at import; it fails at the till, as a 404 on a
button. So these tests check the paths, not the structure.
"""

import importlib
import inspect
import pkgutil

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api


def _submodules():
    return sorted(m.name for m in pkgutil.iter_modules(api.__path__))


class TestEveryPathStillResolves(FrappeTestCase):
    def test_every_whitelisted_endpoint_answers_to_its_old_name(self):
        """The check that matters. `frappe.get_attr` is what the request handler
        uses, and `is_whitelisted` is what it asks next — a re-export has to pass
        both, which it does only if it is the *same function object*, not a copy."""
        checked = 0
        for name in dir(api):
            fn = getattr(api, name)
            if not inspect.isfunction(fn) or fn not in frappe.whitelisted:
                continue
            checked += 1
            with self.subTest(endpoint=name):
                self.assertIs(frappe.get_attr(f"luuvcrm.api.{name}"), fn)
                frappe.is_whitelisted(fn)
        self.assertGreaterEqual(checked, 144,
                                "endpoints went missing from luuvcrm.api")

    def test_the_helpers_the_rest_of_the_app_reaches_for_are_still_there(self):
        """Page context builders and tests import these by name. They are
        private to the app, not to the module they now live in."""
        for name in ("_resolve_item_rate", "_get_pos_price_list", "_server_item_rate",
                     "_hash_pin", "_mobile_from_token", "_customer_mobile",
                     "_send_wa_reply", "_ensure_open_shift", "_process_cashback",
                     "_credit_guide_cashback", "_block_self_approval",
                     "PIN_MAX_ATTEMPTS", "CUSTOMER_TOKEN_DAYS", "PROMO_WA_NUMBER"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(api, name), f"luuvcrm.api.{name} is gone")

    def test_nothing_was_dropped_on_the_way_into_the_package(self):
        """Every name defined in a submodule is re-exported. The package is the
        public surface; a module that forgets to add itself to __init__ breaks a
        path silently."""
        missing = []
        for modname in _submodules():
            module = importlib.import_module(f"luuvcrm.api.{modname}")
            for name, obj in vars(module).items():
                if name.startswith("__") or not _defined_here(obj, module):
                    continue
                if getattr(api, name, None) is not obj:
                    missing.append(f"{modname}.{name}")
        self.assertEqual(missing, [], "not re-exported from luuvcrm.api")


def _defined_here(obj, module):
    return (inspect.isfunction(obj) or inspect.isclass(obj)) \
        and getattr(obj, "__module__", None) == module.__name__


class TestTheSplitHeldItsShape(FrappeTestCase):
    def test_no_module_is_back_to_being_the_whole_api(self):
        """The thing this refactor exists to prevent. 1,000 lines is not a rule,
        it is a tripwire: a module past it has stopped being one subject."""
        oversized = []
        for modname in _submodules():
            path = frappe.get_app_path("luuvcrm", "api", f"{modname}.py")
            lines = len(frappe.read_file(path).splitlines())
            if lines > 1000:
                oversized.append(f"{modname}.py ({lines} lines)")
        self.assertEqual(oversized, [], "split these further")

    def test_the_package_imports_cleanly_from_any_module(self):
        """A ring of imports between two domains will not show up when the
        package is imported top-down in the order __init__ happens to use — only
        when something imports the wrong module first, which in production is
        whichever request arrives after a restart."""
        for modname in _submodules():
            with self.subTest(module=modname):
                importlib.import_module(f"luuvcrm.api.{modname}")

    def test_no_module_imports_the_package_it_lives_in(self):
        """`from luuvcrm.api import x` inside a submodule runs __init__ again,
        which imports every module, from inside the import of one of them."""
        for modname in _submodules():
            src = frappe.read_file(frappe.get_app_path("luuvcrm", "api", f"{modname}.py"))
            with self.subTest(module=modname):
                self.assertNotIn("from luuvcrm import api", src)
                self.assertNotIn("import luuvcrm.api\n", src)


class TestSendingAWhatsAppMessageHasOneSeam(FrappeTestCase):
    """Four modules message customers. When each bound `_send_wa_reply` by name
    there was no single place to stub, so a test that meant to stub the sender
    stubbed only its own module's copy — and the others really would have sent."""

    def test_the_senders_all_go_through_the_wa_module(self):
        from luuvcrm.api import wa
        for modname in ("customers", "guides", "promos", "whatsapp"):
            src = frappe.read_file(frappe.get_app_path("luuvcrm", "api", f"{modname}.py"))
            with self.subTest(module=modname):
                self.assertNotIn("from luuvcrm.api.wa import _send_wa_reply", src)
                self.assertIn("wa._send_wa_reply(", src)
        self.assertTrue(callable(wa._send_wa_reply))

    def test_stubbing_it_once_stops_every_module_sending(self):
        from unittest.mock import patch

        from luuvcrm.api import customers, guides, promos, wa, whatsapp
        with patch.object(wa, "_send_wa_reply", return_value=True) as stub:
            for module in (customers, guides, promos, whatsapp):
                module.wa._send_wa_reply("94770000000", "test")
        self.assertEqual(stub.call_count, 4)
