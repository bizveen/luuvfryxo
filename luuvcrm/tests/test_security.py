"""Regression tests for the Phase 1 security fixes.

Each test names the finding it guards from PRODUCTION_READINESS.md §3. They are
deliberately about the *shape* of the API surface — which decorators, which
guards, where a rate comes from — because that is what silently regresses when
somebody copies an existing endpoint as a template for a new one.

Run: bench --site <site> run-tests --app luuvcrm
"""

import inspect
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api


def _source(fn):
    return inspect.getsource(fn)


class TestOrderEndpointAuth(FrappeTestCase):
    """S1 — update_order was allow_guest with ignore_permissions, so anyone who
    guessed an order name could rewrite an open bill."""

    def test_update_order_is_not_guest_accessible(self):
        self.assertNotIn(api.update_order, frappe.guest_methods,
                         "update_order must not be reachable by Guest")

    def test_update_order_requires_a_pos_role(self):
        self.assertIn("_require_pos_role()", _source(api.update_order),
                      "update_order must guard on a POS role")

    def test_order_changelog_requires_a_pos_role(self):
        """S7 — the changelog names the staff member behind every edit."""
        self.assertNotIn(api.get_order_changelog, frappe.guest_methods)
        self.assertIn("_require_pos_role()", _source(api.get_order_changelog))


class TestPricesComeFromTheServer(FrappeTestCase):
    """S2 — every order path used to bill whatever rate the client posted, so
    `rate: 1` on a Rs. 1600 item produced a Rs. 1 invoice."""

    ORDER_PATHS = [
        api.place_order,
        api.send_to_kitchen,
        api.waiter_append_items,
        api.update_order,
        api.kiosk_place_order,
        api.sync_offline_orders,
    ]

    def test_no_order_path_reads_a_rate_from_the_client(self):
        pattern = re.compile(r'rate\s*=\s*float\(\s*item_data\.get\(\s*["\']rate["\']')
        for fn in self.ORDER_PATHS:
            with self.subTest(endpoint=fn.__name__):
                self.assertIsNone(pattern.search(_source(fn)),
                                  f"{fn.__name__} takes the item rate from the request")

    def test_order_paths_resolve_the_rate_server_side(self):
        for fn in self.ORDER_PATHS:
            with self.subTest(endpoint=fn.__name__):
                self.assertIn("_server_item_rate(", _source(fn),
                              f"{fn.__name__} must price items with _server_item_rate")

    def test_server_rate_falls_back_to_the_menu_row(self):
        """An item priced only on the Restaurant Menu row must not resolve to 0 —
        that fallback is why the client rate existed in the first place."""
        src = _source(api._server_item_rate)
        self.assertIn("_menu_row_rates()", src)
        self.assertIn("_get_pos_price_list()", src)


class TestCustomerDataIsTokenGated(FrappeTestCase):
    """S3 — customer_get_orders accepted a bare mobile number and returned that
    customer's whole history to anyone who asked."""

    def test_order_history_has_no_mobile_fallback(self):
        src = _source(api.customer_get_orders)
        self.assertIn("_mobile_from_token(", src)
        self.assertNotIn('data.get("mobile"', src,
                         "customer_get_orders must not accept a bare mobile number")

    def test_expired_tokens_are_rejected(self):
        """S8 — expiry used to depend on a DELETE that ran only when somebody
        else logged in, so an idle token stayed valid indefinitely."""
        self.assertIn("CUSTOMER_TOKEN_DAYS", _source(api._mobile_from_token))

    def test_unknown_token_returns_nothing(self):
        self.assertIsNone(api._mobile_from_token("not-a-real-token"))
        self.assertIsNone(api._mobile_from_token(""))
        self.assertIsNone(api._mobile_from_token(None))


class TestCustomerPin(FrappeTestCase):
    """S6 — a 4-digit PIN compared in plaintext with no attempt limit."""

    def test_pin_is_stored_hashed(self):
        stored = api._hash_pin("1234")
        self.assertIn("$", stored)
        self.assertNotIn("1234", stored)

    def test_hash_is_salted(self):
        self.assertNotEqual(api._hash_pin("1234"), api._hash_pin("1234"))

    def test_verify_accepts_the_right_pin_and_rejects_the_wrong_one(self):
        stored = api._hash_pin("1234")
        self.assertEqual(api._verify_pin("1234", stored), (True, False))
        self.assertEqual(api._verify_pin("9999", stored), (False, False))

    def test_legacy_plaintext_pin_verifies_and_is_flagged_for_upgrade(self):
        ok, needs_upgrade = api._verify_pin("1234", "1234")
        self.assertTrue(ok)
        self.assertTrue(needs_upgrade, "a plaintext PIN must be upgraded on sign-in")

    def test_new_accounts_never_store_the_pin_in_the_clear(self):
        self.assertIn("_hash_pin(pin)", _source(api.customer_set_pin))

    def test_wrong_pins_lock_the_number_out(self):
        mobile = "0770000000"
        frappe.cache().delete_value(api._pin_attempts_key(mobile))
        self.addCleanup(frappe.cache().delete_value, api._pin_attempts_key(mobile))
        for _ in range(api.PIN_MAX_ATTEMPTS):
            self.assertFalse(api._pin_locked(mobile))
            api._pin_note_failure(mobile)
        self.assertTrue(api._pin_locked(mobile))


class TestManagerReportsAreGuarded(FrappeTestCase):
    """S4 — full revenue, cash drawer and per-bill detail were readable by any
    logged-in user, a waiter or website customer included."""

    MANAGER_ONLY = [
        api.get_manager_dashboard,
        api.get_manager_sales_report,
        api.get_manager_day_report,
        api.get_manager_monthly_sales,
        api.get_manager_bill_detail,
        api.get_manager_purchase_report,
    ]
    STAFF_ONLY = [
        api.get_expenses,
        api.get_due_bills,
        api.get_purchase_bills,
        api.search_purchase_bills,
        api.get_price_book,
    ]

    def test_manager_reports_require_a_manager(self):
        for fn in self.MANAGER_ONLY:
            with self.subTest(endpoint=fn.__name__):
                self.assertIn("_require_manager()", _source(fn))

    def test_purchase_reads_require_a_pos_role(self):
        for fn in self.STAFF_ONLY:
            with self.subTest(endpoint=fn.__name__):
                self.assertIn("_require_pos_role()", _source(fn))

    def test_no_manager_report_is_guest_accessible(self):
        for fn in self.MANAGER_ONLY + self.STAFF_ONLY:
            with self.subTest(endpoint=fn.__name__):
                self.assertNotIn(fn, frappe.guest_methods)


class TestSetupEndpointsAreAdminOnly(FrappeTestCase):
    """S5 — endpoints that create doctypes, custom fields and DocPerms were
    callable by any logged-in user, setup_pos_shift_perms included."""

    def test_every_setup_endpoint_requires_system_manager(self):
        unguarded = []
        for name, fn in vars(api).items():
            if not name.startswith("setup_") or not callable(fn):
                continue
            src = _source(fn)
            if 'only_for("System Manager")' not in src and "_require_" not in src:
                unguarded.append(name)
        self.assertEqual(unguarded, [],
                         f"schema-mutating endpoints without a guard: {unguarded}")

    def test_no_setup_endpoint_is_guest_accessible(self):
        for name, fn in vars(api).items():
            if name.startswith("setup_") and callable(fn):
                with self.subTest(endpoint=name):
                    self.assertNotIn(fn, frappe.guest_methods)


class TestPublicPagesCarryNoPlaceholders(FrappeTestCase):
    """The public menu shipped a wa.me/9470XXXXXXX link, and the manager page
    rendered its PIN into client JavaScript."""

    def test_menu_has_no_placeholder_whatsapp_number(self):
        from luuvcrm.www import menu  # noqa: F401
        path = frappe.get_app_path("luuvcrm", "www", "menu.html")
        with open(path) as fh:
            html = fh.read()
        self.assertNotIn("9470XXXXXXX", html)
        self.assertIn("{% if whatsapp_number %}", html,
                      "the button must not render without a configured number")

    def test_manager_page_does_not_ship_a_pin(self):
        path = frappe.get_app_path("luuvcrm", "www", "manager.html")
        with open(path) as fh:
            html = fh.read()
        self.assertNotIn("MANAGER_PIN", html)
        self.assertNotIn("4321", html)
