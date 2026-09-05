"""The pages, and the gates on them.

A page module is the one place in this app where a guard can be forgotten
without anything failing: `get_context` that never checks a role still renders,
and the screen simply works for whoever asks. So these tests call the context
builders directly and assert on the refusal, not on the markup.

Only `/pos` for now — the route table in `docs/ROADMAP.md` §2b is not settled,
and a test that pins a gate before the decision is made just has to be rewritten.
"""

import importlib

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import roles


def _user(email, role):
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Page Test",
            "send_welcome_email": 0, "roles": [{"role": role}],
        }).insert(ignore_permissions=True)
    return email


class PageTestCase(FrappeTestCase):
    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def context(self, module):
        ctx = frappe._dict()
        module.get_context(ctx)
        return ctx


class TestThereIsOnePos(PageTestCase):
    """There were three POS pages — `/pos` (superseded), `/pos2` (live) and
    `/pos3` (a UI staging copy). pos3 carried all the tablet, printer-bridge
    and print-template work, so it is what `/pos` now serves; the other two
    files are deleted. This is the test that stops a fourth appearing."""

    def test_pos2_and_pos3_are_gone(self):
        for name in ("luuvcrm.www.pos2", "luuvcrm.www.pos3"):
            with self.subTest(module=name):
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)

    def test_the_one_that_is_left_is_the_complete_one(self):
        """pos3's work, not pos2's: the Google-Docs print templates and the
        add-on column the tablet build needs."""
        pos = importlib.import_module("luuvcrm.www.pos")
        self.assertTrue(hasattr(pos, "get_print_templates"))
        self.assertEqual(set(pos.PRINT_TEMPLATE_DOCS),
                         {"guest_check", "kot", "receipt", "zreport"})

    def test_the_page_asks_for_the_templates_at_the_route_that_exists(self):
        """The endpoint moved with the file. A stale dotted path in the HTML
        fails silently — the page falls back to its built-in layout and the
        manager's Google Doc quietly stops driving the paper."""
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.html"))
        self.assertIn("luuvcrm.www.pos.get_print_templates", html)
        self.assertNotIn("luuvcrm.www.pos3", html)

    def test_the_page_no_longer_carries_the_staging_print_only_switch(self):
        """It existed so the staging copy could not touch the live till. On the
        till itself it is a switch that silently stops the POS taking money."""
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.html"))
        self.assertNotIn("PRINT_ONLY", html)

    def test_the_guest_check_button_saves_the_ticket_before_printing(self):
        """The staging block redeclared `guestCheckClick` as a cut-down version
        that refused unless the order already existed — and a hoisted
        redeclaration wins, so the counter got "Send to kitchen first" instead
        of an autosave. One definition, and it is the one that saves."""
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.html"))
        self.assertEqual(html.count("function guestCheckClick("), 1)
        self.assertIn("await autosaveTicket()", html)

    def test_the_service_worker_caches_the_route_that_exists(self):
        """It cached `/pos` while the live page was `/pos2`, so the offline
        shell was of the wrong screen. Now the two agree."""
        sw = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "sw.js"))
        self.assertIn("'/pos'", sw)
        self.assertNotIn("/pos2", sw)

    def test_the_activity_log_knows_one_pos(self):
        from luuvcrm import activity
        self.assertIn("pos", activity.KNOWN_APPS)
        self.assertNotIn("pos2", activity.KNOWN_APPS)
        self.assertNotIn("pos3", activity.KNOWN_APPS)


class TestTheGateOnPos(PageTestCase):
    def setUp(self):
        super().setUp()
        self.pos = importlib.import_module("luuvcrm.www.pos")

    def test_a_guest_is_sent_to_the_pos_login_and_back(self):
        """Back to /pos, not to whichever page the copy was made from — a
        redirect that still named /pos3 would bounce the cashier to a 404."""
        self.as_user("Guest")
        with self.assertRaises(frappe.Redirect):
            self.context(self.pos)
        self.assertEqual(frappe.local.flags.redirect_location,
                         "/pos-login?redirect=/pos")

    def test_someone_with_no_pos_role_is_refused(self):
        self.as_user(_user("lf-test-page-outsider@example.com", roles.KITCHEN_STAFF))
        with self.assertRaises(frappe.PermissionError):
            self.context(self.pos)

    def test_a_cashier_gets_the_screen(self):
        self.as_user(_user("lf-test-page-cashier@example.com", roles.CASHIER))
        ctx = self.context(self.pos)
        self.assertTrue(ctx.csrf_token)
        self.assertIsInstance(ctx.tables, list)
        self.assertIsInstance(ctx.menu_groups, list)
        self.assertIsInstance(ctx.pos_profiles, list)

    def test_the_screen_never_arrives_with_an_empty_menu(self):
        """pos3 added a fallback to the Item master for a site whose Restaurant
        Menu is missing or disabled. A cashier facing an empty till cannot
        sell anything, so it is worth the extra query."""
        self.as_user(_user("lf-test-page-cashier@example.com", roles.CASHIER))
        ctx = self.context(self.pos)
        if frappe.db.count("Item", {"disabled": 0}):
            self.assertTrue(ctx.items_flat, "no items reached the POS screen")
