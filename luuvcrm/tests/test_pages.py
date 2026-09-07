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
    #:
    #: Every staff route in the table in docs/ROADMAP.md §2b is here now. The
    #: three that were added last were all broken, in three different ways —
    #: see TestTheManagerScreensWereUnreachable below.
    GATES = {
        "waiter":    (roles.WAITER, roles.KITCHEN_STAFF),
        "kitchen":   (roles.KITCHEN_STAFF, None),
        "purchases": (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "qrpromo":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "qrcodes":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "manager":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "activity":  (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "errors":    (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "admin":     (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "owner":     (roles.RESTAURANT_OWNER, roles.RESTAURANT_MANAGER),
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

    def test_a_manager_gets_into_everything_except_the_owners_screen(self):
        """The role sets are cumulative on purpose — a manager covering the
        floor must not be locked out of the floor. `/owner` is the one
        deliberate exception: it compares branches and moves tills between
        them, which is the owner's call, not a branch manager's."""
        self.as_user(_as(roles.RESTAURANT_MANAGER))
        for name in self.GATES:
            with self.subTest(page=name):
                if name == "owner":
                    with self.assertRaises(frappe.PermissionError):
                        self.context(self.page(name))
                    continue
                self.context(self.page(name))

    def test_qrcodes_is_the_table_map_and_is_no_longer_public(self):
        """It lists every table and the kiosk URL that orders against it, so
        while it was public anyone could order to any table from anywhere."""
        self.as_user(_as(roles.RESTAURANT_MANAGER))
        ctx = self.context(self.page("qrcodes"))
        self.assertIsInstance(ctx.qr_list, list)
        for row in ctx.qr_list:
            self.assertIn("/kiosk?table=", row["url"])


class TestTheManagerScreensWereUnreachable(PageTestCase):
    """The three routes the gate table had marked ✅ and never had a test.

    All three were broken, and none of them by the gate being missing — which
    is why reading the code had not found them. A gate can also be wrong by
    naming the wrong role, or by not surviving a refactor somewhere else.
    """

    def test_the_activity_page_imports(self):
        """It did not. `www/activity.py` did `from luuvcrm.activity import
        MANAGER_ROLES`, and luuvcrm.activity stopped defining it when the four
        role sets were consolidated into luuvcrm.roles — so the module raised
        ImportError before get_context was ever called and /activity was a 500
        for everybody, manager included. Nothing imports a page module except
        the renderer, so nothing else failed."""
        importlib.import_module("luuvcrm.www.activity")

    def test_the_owner_can_open_the_admin_hub_and_the_error_log(self):
        """Both gated on `System Manager`, which by this app's own rule the
        restaurant owner must not hold: it means "can change how the system
        works" and bypasses every guard in roles.py. So the launcher for every
        screen, and the log that says what is breaking on the floor phones,
        were closed to the person who owns the restaurant."""
        self.as_user(_as(roles.RESTAURANT_OWNER))
        for name in ("admin", "errors"):
            with self.subTest(page=name):
                self.context(importlib.import_module(f"luuvcrm.www.{name}"))

    def test_no_page_names_a_role_of_its_own(self):
        """`www/pos.py` did: a set literal naming System Manager, POS User,
        Sales User, Cashier and Administrator, with Restaurant Manager and
        Restaurant Owner missing — the drift the one-module rule exists to
        stop, in the one file test_roles.TestNoDuplicateRoleSets did not walk.
        A page gates through luuvcrm.roles or it does not gate."""
        import pathlib

        names = (roles.CASHIER, roles.WAITER, roles.KITCHEN_STAFF,
                 roles.RESTAURANT_MANAGER, roles.RESTAURANT_OWNER,
                 roles.SYSTEM_MANAGER, roles.POS_USER, roles.SALES_USER)
        offenders = []
        for path in sorted(pathlib.Path(frappe.get_app_path("luuvcrm", "www")).glob("*.py")):
            body = path.read_text()
            # A role name in a comment is documentation; one in code is a rule
            # written down twice.
            code = "\n".join(line.split("#")[0] for line in body.splitlines())
            for role in names:
                if f"'{role}'" in code or f'"{role}"' in code:
                    offenders.append(f"{path.name}: {role}")
        self.assertEqual(offenders, [],
                         f"page modules naming roles directly: {offenders}")


class TestTheGuideIsNotGatedOnASession(PageTestCase):
    """Guides are external partners with no Frappe User at all — they are
    invited from the POS payment dialog by a wa.me message carrying a password.
    Auth is that token, checked by the guide_* endpoints. A session gate here
    would lock out every single guide, which is the whole audience."""

    def test_a_guest_gets_the_page(self):
        self.as_user("Guest")
        ctx = self.context(importlib.import_module("luuvcrm.www.guide"))
        self.assertTrue(ctx.csrf_token)

    def test_it_does_not_ask_for_a_login(self):
        guide = importlib.import_module("luuvcrm.www.guide")
        self.assertIs(guide.login_required, False)


class TestThePublicPagesStayPublic(PageTestCase):
    """The other half of a gate test, and the half that fails quietly: a page a
    customer reaches from a QR sticker or a WhatsApp link has no session and
    never will. A staff-login redirect added to one of these does not throw
    anywhere a developer is looking — it just sends every customer to a login
    screen for an account they do not have."""

    #: route -> page module. /pos-login is here too: a login page that requires
    #: a login is a door locked from the inside.
    PUBLIC = ("menu", "restaurant_home", "kiosk", "order_online", "claim", "track",
              "cus", "pos-login")

    def module(self, name):
        # "pos-login" is a legal page file and an illegal identifier; import_module
        # takes the name as written.
        return importlib.import_module(f"luuvcrm.www.{name}")

    def test_a_guest_is_never_sent_to_a_staff_login(self):
        self.as_user("Guest")
        for name in self.PUBLIC:
            with self.subTest(page=name):
                module = self.module(name)
                try:
                    self.context(module)
                except frappe.Redirect:
                    self.assertNotIn("/pos-login",
                                     frappe.local.flags.redirect_location or "",
                                     f"/{name} sends customers to the staff login")
                except frappe.PermissionError:
                    self.fail(f"/{name} refuses a guest, and it is a public page")

    def test_none_of_them_requires_a_login(self):
        for name in self.PUBLIC:
            with self.subTest(page=name):
                module = self.module(name)
                self.assertIsNot(getattr(module, "login_required", False), True,
                                 f"/{name} is reached without a session")


class TestNoPageIsShadowedByAnotherApp(PageTestCase):
    """Frappe resolves a page by walking `reversed(get_installed_apps())`, so the
    app installed *last* wins a name — and erpnext is installed after this one.
    Two of our pages had already lost, and both failures were the quiet kind:

    - **`/order` was a 500.** erpnext's `templates/pages/order.py` calls
      `frappe.get_doc(form_dict.doctype, form_dict.name)` with nothing in the
      form dict and raises `ValueError`. `/home` and `/track` both put an
      "Order Online" button in front of customers, and it went to a stack trace.
    - **`/home` was erpnext's website home.** 200, the wrong page, and the
      restaurant's own 494-line landing page had never once been served.

    The route table in the guide had both of them marked ✅, because a route
    table records what an app *declares*, and this is decided by what the site
    *resolves*. So the check has to ask the resolver.
    """

    def resolved_app(self, route):
        """Which app answers this URL, asked of the real resolver.

        `website_route_rules` are matched by werkzeug against
        `frappe.local.request.environ`, and a test has no request — so without
        this the rules are skipped and the check quietly measures the wrong
        thing. Building an environ is what makes the assertion mean what it
        says."""
        from werkzeug.test import EnvironBuilder

        from frappe.website.path_resolver import PathResolver

        previous = getattr(frappe.local, "request", None)
        frappe.local.request = EnvironBuilder(path="/" + route).get_request()
        try:
            _endpoint, renderer = PathResolver(route).resolve()
            return getattr(renderer, "app", None), getattr(renderer, "template_path", "")
        finally:
            frappe.local.request = previous

    def test_every_luuvcrm_page_is_served_by_luuvcrm(self):
        import pathlib

        www = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))
        shadowed = []
        for template in sorted(www.glob("*.html")):
            route = template.stem
            app, path = self.resolved_app(route)
            if app != "luuvcrm":
                shadowed.append(f"/{route} → {app}:{path}")
        self.assertEqual(shadowed, [], f"another app answers these: {shadowed}")

    def test_the_two_public_urls_still_point_at_our_pages(self):
        """The files are named `order-online` and `restaurant-home` now, which
        nothing else claims, and `website_route_rules` keeps the public URLs
        exactly as they were — every link already on a sticker or in a WhatsApp
        message still works."""
        for route, expected in (("order", "order-online"), ("home", "restaurant-home")):
            with self.subTest(route=route):
                app, path = self.resolved_app(route)
                self.assertEqual(app, "luuvcrm")
                self.assertIn(expected, path)

    def test_the_site_root_is_not_the_page_that_was_deleted(self):
        """Website Settings still pointed `/` at `app2`, deleted in the
        September cleanup, so the restaurant's front door was a 404. The
        `home_page` hook is read before Website Settings, which is what makes
        this a code fix rather than something somebody has to remember to click.
        """
        from frappe.website.utils import get_home_page

        self.assertEqual(frappe.get_hooks("home_page"), ["home"])
        self.assertNotEqual(get_home_page(), "app2")

    def test_the_landing_page_is_the_restaurant_one(self):
        page = importlib.import_module("luuvcrm.www.restaurant_home")
        ctx = frappe._dict()
        self.as_user("Guest")
        page.get_context(ctx)
        self.assertIn("menu_groups", ctx,
                      "the landing page shows the menu; erpnext's does not")


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


class TestTheServiceWorkers(PageTestCase):
    """`sw.js` is the offline half of the three staff screens, and every part of
    it was broken in a way that only shows up when the wifi drops — which is the
    only moment it exists for. Online, a service worker that does nothing looks
    exactly like one that works.
    """

    SW = ("sw.js", "cus_sw.js", "manager_sw.js", "purchases_sw.js")

    def sw(self, name):
        """The worker's code, without its comments — the comments name the very
        strings these tests refuse, because that is where the reason lives."""
        body = frappe.read_file(frappe.get_app_path("luuvcrm", "www", name))
        return "\n".join(line.split("//")[0] for line in body.splitlines())

    def test_nothing_is_precached_that_would_404(self):
        """`addAll()` is atomic. sw.js listed '/icon.svg' and '/manifest.json'
        alongside the three screens and neither exists, so the call rejected,
        the .catch() around it swallowed the rejection, and the till precached
        nothing at all."""
        body = self.sw("sw.js")
        for dead in ("'/icon.svg'", "'/manifest.json'"):
            self.assertNotIn(dead, body, f"{dead} is a 404 and takes the shell with it")

    def test_one_bad_url_cannot_empty_the_shell(self):
        self.assertNotIn("addAll(", self.sw("sw.js"),
                         "precache one url at a time so a failure is one screen, not all of them")

    def test_the_shell_is_the_screens_that_exist(self):
        body = self.sw("sw.js")
        for route in ("/pos", "/waiter", "/kitchen"):
            self.assertIn(f"'{route}'", body)
        self.assertNotIn("'/pos2'", body)
        self.assertNotIn("'/pos3'", body)

    def test_the_read_cache_reads_the_path_off_the_url_not_the_request(self):
        """`isApiGet(e.request)` did `url.pathname` on a Request, which has no
        such property. The TypeError killed the fetch listener before
        respondWith(), so the request quietly went to the network — perfect
        online, and offline the menu and the table map were simply gone."""
        body = self.sw("sw.js")
        self.assertIn("isApiGet(e.request, url)", body)
        self.assertIn("function isApiGet(request, url)", body)

    def test_no_worker_invents_a_success_for_a_write_that_failed(self):
        """sw.js answered a failed POST with a fabricated 201 and the words
        "Queued for sync". Nothing was a queue: the cache it wrote was never
        read, no page registered the 'sync-orders' tag, and nothing listened for
        the TRIGGER_SYNC message. A payment that never reached the server was
        reported to the counter as taken."""
        for name in self.SW:
            with self.subTest(worker=name):
                body = self.sw(name)
                self.assertNotIn("Queued for sync", body)
                self.assertNotIn("status: 201", body)
                self.assertNotIn("sync-orders", body)

    def test_every_screen_that_is_in_the_shell_actually_installs_the_worker(self):
        """Only /waiter registered it, so /pos and /kitchen precached their own
        shell exclusively on a device where somebody had opened the floor app
        first."""
        for page in ("pos", "waiter", "kitchen"):
            with self.subTest(page=page):
                html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
                self.assertIn('navigator.serviceWorker.register("/sw.js"', html)


class TestEveryPwaHasAManifestThatResolves(PageTestCase):
    """A `<link rel="manifest">` pointing at a 404 is not an error anywhere — the
    browser just declines to offer "Add to Home screen", and the tablet keeps
    working in a tab."""

    #: page -> the app key its manifest link must ask for
    LINKS = {"pos": "pos", "waiter": "waiter", "kitchen": "kitchen",
             "manager": "manager", "purchases": "purchases", "cus": "cus"}

    def test_no_page_points_at_the_static_manifest_that_was_deleted(self):
        """/manifest.json never came back after the three static files were
        replaced by an endpoint; kitchen.html and waiter.html still asked for
        it."""
        import pathlib

        pages = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))
        for page in sorted(pages.glob("*.html")):
            with self.subTest(page=page.name):
                self.assertNotIn('href="/manifest.json"', page.read_text())

    def test_each_pwa_asks_the_endpoint_for_its_own_manifest(self):
        for page, app in self.LINKS.items():
            if page == "pos":
                continue  # the till installs from /pos; asserted below with its own app key
            with self.subTest(page=page):
                html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
                self.assertIn(f"luuvcrm.api.manifest?app={app}", html)

    def test_the_endpoint_knows_every_app_a_page_asks_for(self):
        """A page asking for an app key the endpoint has never heard of gets the
        customer app's manifest back — the wrong name, the wrong start url, and
        no error."""
        import re

        from luuvcrm.api import brand

        pages = frappe.get_app_path("luuvcrm", "www")
        import pathlib
        asked = set()
        for page in pathlib.Path(pages).glob("*.html"):
            asked |= set(re.findall(r"luuvcrm\.api\.manifest\?app=(\w+)", page.read_text()))
        unknown = sorted(asked - set(brand.APPS))
        self.assertEqual(unknown, [], f"manifest asked for app keys that do not exist: {unknown}")


class TestPollingStopsWhenNobodyIsLooking(PageTestCase):
    """P6. Nine screens polled on a fixed timer whatever the tab was doing — a
    phone in a pocket and a tablet on a locked screen were both still asking the
    server every few seconds, all day, per device.

    `lfPoll` skips the tick while `document.hidden` and fires once the moment the
    tab comes back, so pausing never costs anyone a stale screen.
    """

    #: page → the shortest interval it polls on, in ms
    POLLERS = {"admin.html": 60000, "errors.html": 30000, "activity.html": 30000,
               "kitchen.html": 10000, "waiter.html": 10000, "purchases.html": 20000,
               "manager.html": 8000, "pos.html": 8000, "owner.html": 60000}

    def source(self, name):
        return frappe.read_file(frappe.get_app_path("luuvcrm", "www", name))

    def test_every_polling_screen_uses_the_guard(self):
        for name in self.POLLERS:
            with self.subTest(page=name):
                html = self.source(name)
                self.assertIn("function lfPoll", html, "the helper is missing")
                self.assertIn("document.hidden", html)

    def test_the_guard_catches_up_when_the_tab_comes_back(self):
        """A guard that only skips leaves the screen stale for a whole interval —
        on the kitchen display that is ten seconds of orders it cannot see."""
        for name in self.POLLERS:
            with self.subTest(page=name):
                self.assertIn("visibilitychange", self.source(name))

    def test_no_server_poll_is_left_on_a_bare_timer(self):
        """A `setInterval` that is left is either a clock or a bounded wait for
        one answer; a repeating fetch on a bare timer is the thing this fixes."""
        import re
        allowed = {
            "pos.html": ["setInterval(tick,1000)",          # the on-screen clock
                         "pollEdit", "pollApproval", "pollDiscount"],  # bounded, 40 tries
            "kitchen.html": ["refreshInterval = setInterval"],         # the countdown label
        }
        for name in self.POLLERS:
            with self.subTest(page=name):
                html = self.source(name)
                for line in html.split("\n"):
                    if "setInterval(" not in line or "function lfPoll" in line:
                        continue
                    if any(a in line for a in allowed.get(name, [])):
                        continue
                    self.assertNotRegex(
                        line.strip(), r"setInterval\(\s*(load|refresh|poll|tick|sync)",
                        f"{name}: a repeating fetch on a bare timer — {line.strip()[:70]}")


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
        that "why was this deleted" has an answer in six months.

        `website_route_rules` is no longer empty: it carries the two rules that
        keep /order and /home on our own pages. What matters is that neither one
        leads back to app2, and that nothing re-wires the session hook that
        would have made /app2 a second Desk."""
        from luuvcrm import hooks

        rules = getattr(hooks, "website_route_rules", []) or []
        self.assertNotIn("app2", str(rules))
        self.assertFalse(getattr(hooks, "on_session_creation", None))

    def test_nothing_shadows_frappes_desk_any_more(self):
        """The reason /app2 existed at all. If a www/app.py ever comes back,
        Desk disappears for every staff user and this fails first."""
        import os
        self.assertFalse(os.path.exists(
            frappe.get_app_path("luuvcrm", "www", "app.py")))
