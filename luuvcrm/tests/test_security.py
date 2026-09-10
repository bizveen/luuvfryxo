"""Regression tests for the Phase 1 security fixes.

Each test names the finding it guards from PRODUCTION_READINESS.md §3. They are
deliberately about the *shape* of the API surface — which decorators, which
guards, where a rate comes from — because that is what silently regresses when
somebody copies an existing endpoint as a template for a new one.

Run: bench --site <site> run-tests --app luuvcrm
"""

import inspect
import json
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


def _source(fn):
    return inspect.getsource(fn)


class TestOrderEndpointAuth(FrappeTestCase):
    """S1 — update_order was allow_guest with ignore_permissions, so anyone who
    guessed an order name could rewrite an open bill."""

    def test_update_order_is_not_guest_accessible(self):
        self.assertNotIn(api.update_order, frappe.guest_methods,
                         "update_order must not be reachable by Guest")

    def test_update_order_requires_a_pos_role(self):
        self.assertIn("roles.require_pos()", _source(api.update_order),
                      "update_order must guard on a POS role")

    def test_order_changelog_requires_a_pos_role(self):
        """S7 — the changelog names the staff member behind every edit."""
        self.assertNotIn(api.get_order_changelog, frappe.guest_methods)
        self.assertIn("roles.require_pos()", _source(api.get_order_changelog))


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
                self.assertIn("roles.require_manager()", _source(fn))

    #: The guard each staff read must carry. Purchasing is a role of its own
    #: since 2026-09-10, so the purchase reads ask for it rather than for any POS
    #: role; the expenses list is shared by the till and purchasing. What this
    #: test protects is unchanged — none of them is open to just anybody — only
    #: the name of the guard moved.
    STAFF_GUARD = {
        "get_expenses": "roles.require_expenses()",
        "get_due_bills": "roles.require_purchases()",
        "get_purchase_bills": "roles.require_purchases()",
        "search_purchase_bills": "roles.require_purchases()",
        "get_price_book": "roles.require_purchases()",
    }

    def test_staff_reads_carry_their_guard(self):
        for fn in self.STAFF_ONLY:
            with self.subTest(endpoint=fn.__name__):
                self.assertIn(self.STAFF_GUARD[fn.__name__], _source(fn))

    def test_no_purchase_read_is_open_to_the_till(self):
        """The narrowing itself: a cashier who is not a Purchaser is refused."""
        for fn in self.STAFF_ONLY:
            with self.subTest(endpoint=fn.__name__):
                self.assertNotIn("roles.require_pos()", _source(fn))

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
            if 'only_for("System Manager")' not in src and "roles.require_" not in src:
                unguarded.append(name)
        self.assertEqual(unguarded, [],
                         f"schema-mutating endpoints without a guard: {unguarded}")

    def test_no_endpoint_bootstraps_itself_through_a_guarded_setup(self):
        """The guard belongs on the HTTP wrapper, not on the work.

        Nineteen ordinary endpoints create their own doctype or field on first
        use — create_approval, create_expense, create_wastage, and two GUEST
        endpoints among them. When the guard sat on the function they called,
        the first cashier to request an approval on a fresh site got a
        PermissionError, and so did a guest updating their profile. The work
        lives in _install_* now, and only the whitelisted setup_* wrapper
        checks for System Manager.
        """
        import re
        source = inspect.getsource(api)
        offenders = []
        for match in re.finditer(r"^def (\w+)\(", source, re.M):
            name = match.group(1)
            if name.startswith("setup_"):
                continue
            fn = getattr(api, name, None)
            if not callable(fn):
                continue
            body = "\n".join(_source(fn).split("\n")[1:])
            for called in re.findall(r"(?<![\w.])(setup_\w+)\(\)", body):
                offenders.append(f"{name} -> {called}")
        self.assertEqual(offenders, [],
                         f"these would need admin rights to work: {offenders}")

    def test_the_wrapper_and_the_work_stay_in_step(self):
        """Every _install_* has a setup_* wrapper, and vice versa — a wrapper
        without work, or work no admin can trigger, is a loose end."""
        installs = {n[len("_install_"):] for n in vars(api) if n.startswith("_install_")}
        wrapped = {n[len("setup_"):] for n in vars(api)
                   if n.startswith("setup_") and "_install_" in _source(getattr(api, n))}
        self.assertEqual(installs, wrapped, "the split is uneven")

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


class TestTrackIsNoLongerEnumerable(FrappeTestCase):
    """S7b — the last of the Phase 1 findings.

    `/track` took a bare order name, and order names are a guessable sequence:
    `POS-00104`, `POS-00105`. Walking them read back what every table ate and
    the customer name on the order. A guest now has to present a signature
    keyed on the site's own encryption key, which is what turns "knows the
    format" into "was given this link".

    It was deferred on the grounds that it changes every WhatsApp link already
    in customers' hands. That turned out not to be true — no message sends a
    /track link; the one that builds a customer-facing tracking link sends them
    to /cus, and /admin opens /track as staff, who are exempt.
    """

    def setUp(self):
        super().setUp()
        self.burger = fx.item(self, "TRK-BURGER")
        fx.item_price(self, self.burger, 400)
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(
            items=json.dumps([{"item": self.burger, "qty": 2}]), order_type="Take Away")
        try:
            result = api.send_to_kitchen()
        finally:
            frappe.local.form_dict = previous
        self.order = result["name"] if isinstance(result, dict) else result
        self.addCleanup(lambda: frappe.db.exists("POS Order", self.order) and
                        frappe.delete_doc("POS Order", self.order, force=True,
                                          ignore_permissions=True))

    def as_guest(self):
        previous = frappe.session.user
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, previous)

    def test_a_guest_with_only_the_order_name_gets_nothing(self):
        self.as_guest()
        self.assertEqual(api.get_order_items(self.order)["items"], [])

    def test_a_guest_with_the_signature_gets_the_receipt(self):
        sig = api.track_signature(self.order)
        self.as_guest()
        items = api.get_order_items(self.order, s=sig)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 2)

    def test_a_wrong_signature_is_refused(self):
        self.as_guest()
        self.assertEqual(api.get_order_items(self.order, s="0" * 32)["items"], [])

    def test_one_order_s_signature_does_not_open_another(self):
        """Otherwise a customer with one real link reads every order."""
        other = api.track_signature("POS-00001")
        self.as_guest()
        self.assertEqual(api.get_order_items(self.order, s=other)["items"], [])

    def test_a_missing_order_answers_exactly_like_an_unsigned_one(self):
        """Telling the two apart is most of what an enumerator wants: it turns
        a guess into a confirmed order number."""
        self.as_guest()
        self.assertEqual(api.get_order_items(self.order),
                         api.get_order_items("POS-does-not-exist"))

    def test_staff_still_open_it_without_a_signature(self):
        """/admin links /track bare, and a cashier checking an order should not
        need to construct a link."""
        self.assertTrue(api.get_order_items(self.order)["items"])

    def test_the_signature_is_not_derivable_from_the_name(self):
        from frappe.utils.password import get_encryption_key
        sig = api.track_signature(self.order)
        self.assertEqual(len(sig), 32)
        self.assertNotIn(self.order.lower(), sig.lower())
        self.assertTrue(get_encryption_key(), "signing key must exist")

    def test_the_link_helper_produces_a_usable_url(self):
        url = api.track_url(self.order)
        self.assertIn("/track?order=", url)
        self.assertIn(f"&s={api.track_signature(self.order)}", url)

    def test_the_page_sends_the_signature_through(self):
        """A signed endpoint and a page that never passes the signature is a
        page that shows every customer an empty order."""
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "track.html"))
        self.assertIn("params.get('s')", html)
        self.assertIn("&s=", html)
