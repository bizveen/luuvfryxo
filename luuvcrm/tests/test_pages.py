"""The pages, and the gates on them.

A page module is the one place in this app where a guard can be forgotten
without anything failing: `get_context` that never checks a role still renders,
and the screen simply works for whoever asks. So these tests call the context
builders directly and assert on the refusal, not on the markup.

The route table in `docs/ROADMAP.md` §2b is settled for the staff pages; the
handful still marked undecided there are left alone, because a gate test written
before the decision just gets rewritten.
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


def _as(role):
    """A throwaway user holding exactly one role."""
    return _user(f"lf-test-page-{role.lower().replace(' ', '-')}@example.com", role)


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

    def test_pos3_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("luuvcrm.www.pos3")

    def test_pos2_is_a_redirect_and_not_a_second_till(self):
        """It survives only as four lines pointing at /pos. A page there again
        means two counter screens drifting apart, which is what this whole
        collapse was for."""
        pos2 = importlib.import_module("luuvcrm.www.pos2")
        self.assertFalse(hasattr(pos2, "get_print_templates"))
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos2.html"))
        self.assertLess(len(html), 400, "pos2.html should be a stub")
        self.assertIn("/pos", html)

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


class TestTheOldPosBookmarkStillLands(PageTestCase):
    """Every counter tablet is bookmarked to `/pos2`, the address the till had
    before the three POS pages became one. The route is kept for one release so
    a bookmark does not 404 on the counter mid-service."""

    def redirect(self, query=b""):
        previous = getattr(frappe.local, "request", None)
        frappe.local.request = frappe._dict(query_string=query)
        self.addCleanup(setattr, frappe.local, "request", previous)
        pos2 = importlib.import_module("luuvcrm.www.pos2")
        with self.assertRaises(frappe.Redirect):
            self.context(pos2)
        return frappe.local.flags.redirect_location

    def test_the_bookmark_lands_on_the_till(self):
        self.assertEqual(self.redirect(), "/pos")

    def test_the_query_string_survives(self):
        self.assertEqual(self.redirect(b"table=T4"), "/pos?table=T4")

    def test_it_goes_through_the_one_redirect_in_pos(self):
        """A second copy of the same lines is how one of them ends up dropping
        the query string and nobody notices."""
        from luuvcrm.www import pos
        pos2 = importlib.import_module("luuvcrm.www.pos2")
        self.assertIs(pos2.redirect_to_pos, pos.redirect_to_pos)


class TestTheOldCustomerRoutesStillLand(PageTestCase):
    """`/customer`, `/myorders` and `/my_orders` all became `/cus`.

    They are not deleted, and should not be: each one is a link already sitting
    in somebody's WhatsApp thread or printed on a QR sticker. What they are is
    four lines of redirect each — the 530- and 311-line copies of the old order
    page that sat behind two of them never rendered, because `get_context`
    raises before the template is reached.
    """

    ALIASES = ("customer", "myorders", "my_orders")

    def alias(self, name):
        return importlib.import_module(f"luuvcrm.www.{name}")

    def redirect(self, name, query=b""):
        """Call the alias the way a request does, and return where it points."""
        previous = getattr(frappe.local, "request", None)
        frappe.local.request = frappe._dict(query_string=query)
        self.addCleanup(setattr, frappe.local, "request", previous)
        with self.assertRaises(frappe.Redirect):
            self.context(self.alias(name))
        return frappe.local.flags.redirect_location

    def test_every_old_route_lands_on_cus(self):
        for name in self.ALIASES:
            with self.subTest(route=name):
                self.assertEqual(self.redirect(name), "/cus")

    def test_the_deep_link_survives_the_hop(self):
        """?phone= is from a WhatsApp link, ?ref= from a referral, ?promo= from
        a sticker QR. Dropping the query string turns a personalised link into
        a generic one — the customer lands in the app as a stranger."""
        for name in self.ALIASES:
            with self.subTest(route=name):
                self.assertEqual(self.redirect(name, b"phone=0770000001&ref=ABC"),
                                 "/cus?phone=0770000001&ref=ABC")

    def test_they_all_go_through_one_redirect(self):
        """Three copies of the same eight lines is how one of them ends up
        dropping the query string and nobody notices."""
        from luuvcrm.www import cus
        for name in self.ALIASES:
            with self.subTest(route=name):
                self.assertIs(self.alias(name).redirect_to_cus, cus.redirect_to_cus)

    def test_no_stale_copy_of_the_old_page_is_left_behind(self):
        """The .html sibling exists only so the route resolves; the redirect
        fires first. A full page there is dead code that reads as live."""
        for name in self.ALIASES:
            with self.subTest(route=name):
                html = frappe.read_file(
                    frappe.get_app_path("luuvcrm", "www", f"{name}.html"))
                self.assertLess(len(html), 400, f"{name}.html should be a stub")
                self.assertIn("/cus", html)

    def test_the_whatsapp_auto_reply_links_to_the_app_itself(self):
        """It pointed at /myorders, which only bounces to /cus. One hop fewer,
        and the link in the thread stays right if the alias is ever dropped."""
        source = frappe.read_file(
            frappe.get_app_path("luuvcrm", "api", "whatsapp.py"))
        self.assertIn('"View your orders: " + settings.url("/cus")', source)
        self.assertNotIn("/myorders", source)


class TestTheStaffPagesAreGated(PageTestCase):
    """A page module is the one place a guard can be forgotten without anything
    failing: a `get_context` that never checks a role still renders, and the
    screen simply works for whoever asks.

    Four of these had a login and no role check, and `/qrcodes` had neither.
    Each case below names what the page actually gives away.
    """

    #: page module -> (the lowest role that should get in, a role that should not)
    GATES = {
        "waiter":    (roles.WAITER, roles.KITCHEN_STAFF),
        "kitchen":   (roles.KITCHEN_STAFF, None),
        "purchases": (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "qrpromo":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "qrcodes":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
    }

    def page(self, name):
        return importlib.import_module(f"luuvcrm.www.{name}")

    def test_a_guest_never_reaches_any_of_them(self):
        """/kitchen used to set `redirect_location` and then fall through
        without raising — the flag was set, ignored, and the whole screen
        rendered for anyone who asked. /qrcodes did not even check."""
        self.as_user("Guest")
        for name in self.GATES:
            with self.subTest(page=name):
                if name == "waiter":
                    # /waiter answers a guest with an inline login form rather
                    # than a redirect, and must not build the menu.
                    ctx = self.context(self.page(name))
                    self.assertTrue(ctx.get("is_login"))
                    self.assertIsNone(ctx.get("menu_groups"))
                    continue
                with self.assertRaises(frappe.Redirect):
                    self.context(self.page(name))
                self.assertIn("/pos-login?redirect=/", frappe.local.flags.redirect_location)

    def test_the_role_the_page_is_for_gets_in(self):
        for name, (allowed, _denied) in self.GATES.items():
            with self.subTest(page=name, role=allowed):
                self.as_user(_as(allowed))
                self.context(self.page(name))

    def test_a_role_the_page_is_not_for_is_refused(self):
        for name, (_allowed, denied) in self.GATES.items():
            if denied is None:
                continue
            with self.subTest(page=name, role=denied):
                self.as_user(_as(denied))
                with self.assertRaises(frappe.PermissionError):
                    self.context(self.page(name))

    def test_a_manager_gets_into_everything(self):
        """The role sets are cumulative on purpose — a manager covering the
        floor must not be locked out of the floor."""
        self.as_user(_as(roles.RESTAURANT_MANAGER))
        for name in self.GATES:
            with self.subTest(page=name):
                self.context(self.page(name))

    def test_qrcodes_is_the_table_map_and_is_no_longer_public(self):
        """It lists every table and the kiosk URL that orders against it, so
        while it was public anyone could order to any table from anywhere."""
        self.as_user(_as(roles.RESTAURANT_MANAGER))
        ctx = self.context(self.page("qrcodes"))
        self.assertIsInstance(ctx.qr_list, list)
        for row in ctx.qr_list:
            self.assertIn("/kiosk?table=", row["url"])


class TestEveryStaffPageReportsItsErrors(PageTestCase):
    """`lferr.js` is what turns "the app is being weird" into a row somebody can
    read. It was on six pages and missing from the two the floor and the kitchen
    actually hold — which is where the reports are worth most."""

    STAFF_PAGES = ("pos", "waiter", "kitchen", "manager", "purchases", "admin",
                   "activity", "errors")

    def test_the_reporter_is_loaded(self):
        for page in self.STAFF_PAGES:
            html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
            with self.subTest(page=page):
                self.assertIn("/lferr.js", html)

    def test_it_tags_the_rows_with_an_app_the_log_knows(self):
        """An unrecognised `data-app` is bucketed as "other" and the /errors app
        chips stop being able to separate one screen from another."""
        import re

        from luuvcrm import activity
        for page in self.STAFF_PAGES:
            html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
            found = re.search(r'lferr\.js[^>]*data-app="([^"]+)"', html)
            with self.subTest(page=page):
                self.assertIsNotNone(found, f"{page} loads lferr.js without a data-app")
                self.assertIn(found.group(1), activity.KNOWN_APPS)

    def test_it_can_reach_the_endpoint_it_posts_to(self):
        js = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "lferr.js"))
        self.assertIn("/api/method/luuvcrm.api.log_client_error", js)
        frappe.is_whitelisted(frappe.get_attr("luuvcrm.api.log_client_error"))


class TestApp2IsGone(PageTestCase):
    """`/app2` was a second copy of Frappe Desk.

    `luuvcrm/www/app.py` — the customer PWA — used to sit on `/app` and, because
    luuvcrm resolves before frappe, shadowed Desk completely; `/app2` re-hosted
    Desk somewhere reachable. That file is gone, so nothing shadows `/app` any
    more. `/app2` was also only ever half-wired: it needed `website_route_rules`
    and `on_session_creation` in `hooks.py` and neither was ever added, so
    `redirect_desk_login` never ran and `/app2/<doctype>` 404'd.
    """

    def test_the_module_is_gone(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("luuvcrm.www.app2")

    def test_nothing_is_left_pointing_at_it(self):
        from luuvcrm import activity
        self.assertNotIn("app2", activity.KNOWN_APPS)

    def test_the_hooks_it_needed_were_never_wired(self):
        """The evidence it was vestigial rather than merely unused — recorded so
        that "why was this deleted" has an answer in six months."""
        from luuvcrm import hooks
        self.assertFalse(getattr(hooks, "website_route_rules", None))
        self.assertFalse(getattr(hooks, "on_session_creation", None))

    def test_nothing_shadows_frappes_desk_any_more(self):
        """The reason /app2 existed at all. If a www/app.py ever comes back,
        Desk disappears for every staff user and this fails first."""
        import os
        self.assertFalse(os.path.exists(
            frappe.get_app_path("luuvcrm", "www", "app.py")))
