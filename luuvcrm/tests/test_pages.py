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
import pathlib
from urllib.parse import urlencode
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import roles
from luuvcrm.tests import fixtures as fx


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

    def test_the_staging_copy_is_not_a_page_any_more(self):
        """`/pos3` was the UI staging copy, and it is the file that became
        `/pos` — it carried the tablet layer, the printer bridge and the print
        layouts. Deleting the route outright left every bookmark and every
        history entry pointing at it answering 404 on a till, so it is a
        redirect now. What must not come back is a *screen* there."""
        pos3 = importlib.import_module("luuvcrm.www.pos3")
        self.assertFalse(hasattr(pos3, "get_print_templates"))
        self.assertFalse(pathlib.Path(
            frappe.get_app_path("luuvcrm", "www", "pos3.html")).read_text().count("<script"))

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
    """The till has had three addresses: `/pos2` was the live one and `/pos3`
    was the staging copy that became `/pos`. Both are bookmarked on counter
    tablets and both are in somebody's history, so both are kept for one release
    — a 404 mid-service costs a queue at the counter, and a redirect costs four
    lines."""

    OLD = ("pos2", "pos3")

    def redirect(self, page="pos2", query=b""):
        previous = getattr(frappe.local, "request", None)
        frappe.local.request = frappe._dict(query_string=query)
        self.addCleanup(setattr, frappe.local, "request", previous)
        module = importlib.import_module(f"luuvcrm.www.{page}")
        with self.assertRaises(frappe.Redirect):
            self.context(module)
        return frappe.local.flags.redirect_location

    def test_every_old_address_lands_on_the_till(self):
        for page in self.OLD:
            with self.subTest(page=page):
                self.assertEqual(self.redirect(page), "/pos")

    def test_the_query_string_survives(self):
        """`/pos2?table=T4` is how a bookmark opens on a table. Dropping the
        query string sends the cashier to a till with no table selected, which
        looks like the bookmark working."""
        for page in self.OLD:
            with self.subTest(page=page):
                self.assertEqual(self.redirect(page, b"table=T4"), "/pos?table=T4")

    def test_neither_is_a_second_till(self):
        """A page there again means two counter screens drifting apart, which is
        what the collapse was for."""
        for page in self.OLD:
            with self.subTest(page=page):
                module = importlib.import_module(f"luuvcrm.www.{page}")
                self.assertFalse(hasattr(module, "get_print_templates"))
                html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
                self.assertLess(len(html), 400, f"{page}.html should be a stub")
                self.assertIn("/pos", html)

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
        "purchases": (roles.PURCHASER, roles.CASHIER),
        "qrpromo":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "qrcodes":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "manager":   (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "activity":  (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "errors":    (roles.RESTAURANT_MANAGER, roles.CASHIER),
        "admin":     (roles.RESTAURANT_MANAGER, roles.CASHIER),
    }
    # `/owner` is not here any more: it is a redirect into /admin#branches, and
    # TestTheAdminPortal is what checks it. The owner-only *sections* inside the
    # portal are gated by their endpoints, not by a page.

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
    PUBLIC = ("menu", "home", "kiosk", "order", "claim", "track",
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
    """Frappe resolves a page by walking `reversed(frappe.get_installed_apps())`
    — the app installed *last* wins a name. erpnext ships `home` and `order`,
    and so do we:

    - **`/order`** is erpnext's `templates/pages/order.py` on a bench where
      erpnext came after this app: it does `frappe.get_doc(form_dict.doctype,
      form_dict.name)` with nothing in the form dict and raises. `/home` and
      `/track` both put an "Order Online" button in front of customers.
    - **`/home`** would be erpnext's website home. 200, the wrong page.

    `required_apps` is what settles it: bench installs erpnext and hospitality
    *before* this app, so on every site installed the documented way — the
    live one included — this app resolves after erpnext and both pages are
    ours. There were `website_route_rules` and a `home_page` hook forcing the
    same outcome for a while; they went, because hooks are bench-wide and this
    app shares a bench with a site that is not a restaurant (see hooks.py).

    So the check asks the resolver, with a real request environ — the rules
    were matched by werkzeug against it, and the renderers still are.
    """

    def resolved_app(self, route):
        """Which app answers this URL, asked of the real resolver."""
        from werkzeug.test import EnvironBuilder

        from frappe.website.path_resolver import PathResolver

        previous = getattr(frappe.local, "request", None)
        frappe.local.request = EnvironBuilder(path="/" + route).get_request()
        try:
            _endpoint, renderer = PathResolver(route).resolve()
            return getattr(renderer, "app", None), getattr(renderer, "template_path", "")
        finally:
            frappe.local.request = previous

    def installed_in_the_documented_order(self):
        """True when this app resolves after erpnext — the order `required_apps`
        produces and the order the live site has. A dev bench that installed
        this app first sees erpnext's pages instead; that is the bench's
        problem, not the app's, and the tests below say so rather than fail."""
        apps = frappe.get_installed_apps()
        return "erpnext" not in apps or apps.index("luuvcrm") > apps.index("erpnext")

    def test_every_luuvcrm_page_is_served_by_luuvcrm(self):
        import pathlib

        if not self.installed_in_the_documented_order():
            self.skipTest("erpnext was installed after luuvcrm on this bench; "
                          "reinstall in the required_apps order")
        www = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))
        skip = {p.split("/")[-1] for p in fx.live_only_paths() if p.startswith("www/")}
        shadowed = []
        for template in sorted(www.glob("*.html")):
            if template.name in skip:
                continue          # the clothing site's page, on the host only
            route = template.stem
            app, path = self.resolved_app(route)
            if app != "luuvcrm":
                shadowed.append(f"/{route} → {app}:{path}")
        self.assertEqual(shadowed, [], f"another app answers these: {shadowed}")

    def test_the_two_public_urls_are_our_pages(self):
        """`/order` and `/home` — the two URLs on stickers and in messages —
        are served from `www/order.html` and `www/home.html` by install order
        alone. No route rule, no hook: nothing here can reach the other site."""
        if not self.installed_in_the_documented_order():
            self.skipTest("erpnext was installed after luuvcrm on this bench")
        for route in ("order", "home"):
            with self.subTest(route=route):
                app, path = self.resolved_app(route)
                self.assertEqual(app, "luuvcrm")
                self.assertIn(f"www/{route}.html", path.replace("\\", "/"))

    def test_nothing_bench_wide_decides_a_route(self):
        """The rules and the `home_page` hook would have applied to every site
        on the bench, and one of them is a clothing shop whose `/` is its own
        Web Page. Both stay gone."""
        from luuvcrm import hooks

        self.assertFalse(getattr(hooks, "website_route_rules", None))
        self.assertFalse(getattr(hooks, "home_page", None))
        self.assertEqual(frappe.get_hooks("home_page", app_name="luuvcrm"), [])

    def test_the_site_root_is_not_the_page_that_was_deleted(self):
        """Website Settings pointed `/` at `app2`, deleted in the September
        cleanup, so the restaurant's front door was a 404. A fresh site gets
        `home` from `setup_doctype.ensure_home_page()` (after_install); a site
        that chose something else keeps it."""
        from frappe.website.utils import get_home_page

        from luuvcrm import setup_doctype

        before = frappe.db.get_single_value("Website Settings", "home_page")
        try:
            frappe.db.set_single_value("Website Settings", "home_page", "app2")
            setup_doctype.ensure_home_page()
            self.assertEqual(
                frappe.db.get_single_value("Website Settings", "home_page"), "home")
            frappe.clear_cache()
            self.assertNotEqual(get_home_page(), "app2")

            frappe.db.set_single_value("Website Settings", "home_page", "shop")
            setup_doctype.ensure_home_page()
            self.assertEqual(
                frappe.db.get_single_value("Website Settings", "home_page"), "shop",
                "a site that chose its own front door keeps it")
        finally:
            frappe.db.set_single_value("Website Settings", "home_page", before or "")
            frappe.clear_cache()

    def test_the_landing_page_is_the_restaurant_one(self):
        page = importlib.import_module("luuvcrm.www.home")
        ctx = frappe._dict()
        self.as_user("Guest")
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict()
        try:
            page.get_context(ctx)
        finally:
            frappe.local.form_dict = previous
        self.assertIn("menu_groups", ctx,
                      "the landing page shows the menu; erpnext's does not")

    def test_the_landing_app_bar_stays_visible_while_scrolling(self):
        template = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "home.html"))
        self.assertIn(".nav-wrap{position:fixed", template)
        self.assertIn("z-index:100", template)
        self.assertIn("section[id]{scroll-margin-top:", template)

    def test_the_landing_page_prioritises_kurunegala_and_keeps_branch_links_safe(self):
        """The front page shows both branches, but every CTA carries the
        branch explicitly so a Dambulla order cannot reach Kurunegala."""
        from luuvcrm import branch

        names = branch.all_restaurants()
        if not names:
            self.skipTest("no restaurants configured")

        page = importlib.import_module("luuvcrm.www.home")
        ctx = frappe._dict()
        self.as_user("Guest")
        page.get_context(ctx)

        expected = "Kurunegala" if "Kurunegala" in names else names[0]
        self.assertEqual(ctx.primary_location.name, expected)
        self.assertEqual({row.name for row in ctx.locations}, set(names))
        for row in ctx.locations:
            with self.subTest(restaurant=row.name):
                query = urlencode({"r": row.name})
                self.assertEqual(row.order_url, f"/cus?{query}")
                self.assertEqual(row.menu_url, f"/menu?{query}")
        self.assertEqual(
            ctx.primary_location.rewards_url,
            "/cus?" + urlencode({"r": expected, "tab": "rewards"}),
        )
        if expected == "Kurunegala":
            self.assertEqual(
                [item.name for item in ctx.featured_items],
                [
                    "Chicken Pot Biryani (Single)",
                    "Vegetable Cheese Kottu (Regular)",
                    "Hyderabadi Style Chicken Biryani",
                ],
            )
            public = pathlib.Path(frappe.get_app_path("luuvcrm", "public"))
            for item in ctx.featured_items:
                asset = item.image.removeprefix("/assets/luuvcrm/")
                self.assertTrue((public / asset).is_file(), item.image)

    def test_the_landing_page_carries_customer_deep_links_into_every_cta(self):
        page = importlib.import_module("luuvcrm.www.home")
        previous = frappe.local.form_dict
        try:
            self.as_user("Guest")
            frappe.local.form_dict = frappe._dict(
                ref="LVAB12", promo="LFCD34", phone="0770000123"
            )
            ctx = frappe._dict()
            page.get_context(ctx)
        finally:
            frappe.local.form_dict = previous

        for row in ctx.locations:
            for url in (row.order_url, row.menu_url, row.rewards_url):
                with self.subTest(restaurant=row.name, url=url):
                    self.assertIn("ref=LVAB12", url)
                    self.assertIn("promo=LFCD34", url)
                    self.assertIn("phone=0770000123", url)
            self.assertTrue(row.order_url.startswith("/cus?"))

    def test_public_menu_hides_test_rows_and_has_a_safe_image_fallback(self):
        page = importlib.import_module("luuvcrm.www.menu")
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        if not restaurant:
            self.skipTest("no restaurant configured")
        fake_items = [
            {"code": "REAL-DISH", "rate": 500},
            {"code": "_LFT-LEAKED-DISH", "rate": 1},
        ]
        with patch.object(page.branch, "for_page", return_value=restaurant), \
                patch.object(page, "menu_items", return_value=fake_items):
            context = frappe._dict()
            page.get_context(context)
        self.assertEqual([item["code"] for item in context.menu_items], ["REAL-DISH"])

        template = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "menu.html"))
        self.assertIn("/assets/luuvcrm/app/menu-placeholder.svg", template)
        self.assertIn("this.onerror=null;this.src=", template)
        self.assertIn('class="category-nav"', template)
        self.assertIn("data-category-link", template)
        self.assertIn("data-category-section", template)
        self.assertIn("overflow-x: auto", template)
        for mojibake in ("ð", "Ã", "ï¸"):
            self.assertNotIn(mojibake, template)

        pos_template = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.html"))
        self.assertIn('class="pos-menu-thumb"', pos_template)
        self.assertIn("/assets/luuvcrm/app/menu-placeholder.svg", pos_template)
        self.assertIn("this.onerror=null;this.src=", pos_template)

    def test_referral_share_and_qr_stay_on_the_direct_customer_app(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "cus.html"))
        self.assertIn("window.location.origin+'/cus?ref='", html)
        self.assertNotIn("chart.googleapis.com", html)
        self.assertIn("/assets/luuvcrm/js/qrcode.js", html)

    def test_branch_neutral_customer_links_open_kurunegala_without_a_chooser(self):
        page = importlib.import_module("luuvcrm.www.cus")
        previous = frappe.local.form_dict
        try:
            frappe.local.form_dict = frappe._dict(ref="LVAB12")
            with patch.object(page.branch, "all_restaurants",
                              return_value=["Dambulla", "Kurunegala"]), \
                    patch.object(page.branch, "remember") as remember, \
                    patch.object(page.branch, "for_page") as resolver, \
                    patch.object(page, "menu_groups", return_value=[]):
                context = frappe._dict()
                page.get_context(context)
        finally:
            frappe.local.form_dict = previous
        resolver.assert_not_called()
        remember.assert_called_once_with("Kurunegala")
        self.assertEqual(context.branch, "Kurunegala")
        self.assertEqual(context.branch_choices, [])
        self.assertEqual(context.restaurant_name, "Kurunegala")

    def test_customer_app_keeps_explicit_branch_links(self):
        page = importlib.import_module("luuvcrm.www.cus")
        previous = frappe.local.form_dict
        try:
            frappe.local.form_dict = frappe._dict(r="Dambulla")
            with patch.object(page.branch, "for_page",
                              return_value="Dambulla") as resolver, \
                    patch.object(page, "menu_groups", return_value=[]):
                context = frappe._dict()
                page.get_context(context)
        finally:
            frappe.local.form_dict = previous
        resolver.assert_called_once_with(context)
        self.assertEqual(context.restaurant_name, "Dambulla")

    def test_branch_chooser_is_mobile_stacked_and_marks_the_default(self):
        template = frappe.read_file(frappe.get_app_path(
            "luuvcrm", "templates", "includes", "branch_choose.html"
        ))
        self.assertIn("lf-branch-card{% if choice.is_default %} is-default", template)
        self.assertIn("grid-template-columns:1fr", template)
        self.assertIn("aspect-ratio:1.12/1", template)
        self.assertIn("referral deal saved", template)

    def test_customer_app_accepts_only_known_deep_link_tabs(self):
        page = importlib.import_module("luuvcrm.www.cus")
        previous = frappe.local.form_dict
        try:
            frappe.local.form_dict = frappe._dict(tab="rewards")
            rewards = frappe._dict()
            page.get_context(rewards)
            self.assertEqual(rewards.initial_tab, "offers")

            frappe.local.form_dict = frappe._dict(tab="order")
            order = frappe._dict()
            page.get_context(order)
            self.assertEqual(order.initial_tab, "order")

            frappe.local.form_dict = frappe._dict(tab="not-a-real-tab")
            fallback = frappe._dict()
            page.get_context(fallback)
            self.assertEqual(fallback.initial_tab, "home")
        finally:
            frappe.local.form_dict = previous

    def test_customer_app_has_a_home_status_menu_and_offers_navigation(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "cus.html"))
        self.assertIn('id="tabHome"', html)
        self.assertIn('id="homeStatus"', html)
        self.assertIn('data-tab="order"', html)
        self.assertIn('data-tab="offers"', html)
        self.assertIn('id="offerHighlights"', html)
        self.assertIn('id="searchInput"', html)
        self.assertIn('id="itemDetailOverlay"', html)
        self.assertIn('class="page-hero"', html)
        self.assertIn('class="hero-food', html)
        self.assertIn('class="popular-row"', html)
        self.assertIn('Good food, less waiting.', html)
        self.assertIn("grid-template-columns:minmax(0,1fr)", html)
        self.assertIn('class="desc">{{ item.description }}', html)
        self.assertIn('data-search=', html)
        self.assertIn('<b>+</b> Add', html)
        self.assertEqual(html.count('id="acPin2"'), 1)
        self.assertIn('/assets/luuvcrm/app/menu-placeholder.svg', html)
        self.assertIn("this.onerror=null;this.src=", html)
        self.assertIn("const RESTAURANT =", html)
        self.assertIn("restaurant:RESTAURANT", html)
        self.assertNotIn("Kurunegala", html)
        self.assertIn("function renderHome()", html)
        self.assertIn("function renderOffers()", html)

    def test_customer_top_picks_use_sales_from_the_last_24_hours(self):
        page = importlib.import_module("luuvcrm.www.cus")
        now = object()
        cutoff = object()
        rows = [frappe._dict(item="DISH-1")]

        with patch.object(page, "now_datetime", return_value=now), \
                patch.object(page, "add_days", return_value=cutoff) as add_days, \
                patch.object(page.frappe.db, "sql", return_value=rows) as sql:
            result = page._popular_item_codes("Main Branch", ["DISH-1", "DISH-2"])

        add_days.assert_called_once_with(now, -1)
        query, values = sql.call_args.args[:2]
        self.assertIn("SUM(soi.qty)", query)
        self.assertIn("LIMIT 5", query)
        self.assertEqual(values, (cutoff, "Main Branch", "DISH-1", "DISH-2"))
        self.assertEqual(result, ["DISH-1"])

        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "cus.html"))
        self.assertIn("Top picks at", html)
        self.assertIn("Top 5 sellers from the last 24 hours", html)
        self.assertNotIn("Fresh picks", html)

    def test_customer_top_picks_fall_back_to_five_random_menu_items(self):
        page = importlib.import_module("luuvcrm.www.cus")
        menu_items = [
            {"code": f"DISH-{index}", "group": "Food"}
            for index in range(1, 7)
        ] + [{"code": "EXTRA-1", "group": "Add-ons"}]
        chosen = menu_items[1:6]

        with patch.object(page, "sample", return_value=chosen) as sample:
            result = page._random_menu_picks(menu_items)

        sample.assert_called_once_with(menu_items[:6], 5)
        self.assertEqual(result, chosen)


class TestTheAdminPortal(PageTestCase):
    """/admin was a launcher — tiles that opened seven other screens — and
    everything an owner actually had to *change* was either on /owner or in the
    Frappe Desk, which is a developer's tool wearing a business app's name.

    It is one portal now, and /owner is folded into it. The sections are the
    nouns a restaurant owner uses (branches, staff, menu, items, reports), not
    the doctypes underneath them.
    """

    def page(self):
        return frappe.read_file(frappe.get_app_path("luuvcrm", "www", "admin.html"))

    def context_for(self, role):
        self.as_user(_as(role))
        ctx = frappe._dict()
        importlib.import_module("luuvcrm.www.admin").get_context(ctx)
        return ctx

    def test_every_section_the_owner_needs_is_on_the_page(self):
        html = self.page()
        for panel in ("p-overview", "p-reports", "p-branches", "p-staff",
                      "p-items", "p-menu", "p-apps"):
            with self.subTest(panel=panel):
                self.assertIn(panel, html)

    def test_overview_is_a_decision_dashboard_with_range_and_comparisons(self):
        html = self.page()
        for marker in ("Business overview", 'id="ovRange"', 'id="ovTrend"',
                       'id="ovBars"', 'id="ovInsights"', "get_owner_overview"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_overview_can_show_a_selected_calendar_month(self):
        html = self.page()
        for marker in ('value="month">Selected month', 'type="month" id="ovMonth"',
                       "function monthParams", "function applyOverviewMonth"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_revenue_trend_is_a_day_by_day_bar_chart(self):
        html = self.page()
        self.assertIn("Daily restaurant revenue, side by side", html)
        self.assertIn("data-trend=\"revenue\"", html)
        self.assertIn("data-trend=\"expenses\"", html)
        self.assertIn("function setTrendMode", html)
        self.assertIn('svg+=`<rect', html)
        self.assertIn("groupX+bi*(barW+gap)", html)
        self.assertIn("host.clientWidth", html)
        self.assertIn("TREND_RESIZE_TIMER", html)
        self.assertNotIn('svg+=`<polyline', html)

    def test_overview_remains_useful_on_a_phone(self):
        html = self.page()
        self.assertIn("#cmp th:first-child,#cmp td:first-child{position:sticky", html)
        self.assertIn(".trend-tabs{width:100%", html)
        self.assertIn(".ov-legend{width:100%;overflow-x:auto", html)

    def test_restaurant_comparison_explains_why_branches_differ(self):
        html = self.page()
        for detail in ("Vs previous period", "Business share", "Revenue / day",
                       "Expenses", "Expenses / day", "Approvals waiting", "Growth leader",
                       "Highest average bill", "Needs attention"):
            with self.subTest(detail=detail):
                self.assertIn(detail, html)
        for visual in ("cmp-good", "cmp-bad", "cmp-strong", "spend-start"):
            with self.subTest(visual=visual):
                self.assertIn(visual, html)

    def test_overview_has_owner_decision_charts(self):
        html = self.page()
        for marker in ("Revenue, expenses &amp; net", "Orders &amp; average bill",
                       'id="ovNetChart"', 'id="ovDriverChart"',
                       "function renderNetChart", "function renderDriverChart"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_setup_warnings_live_with_restaurants_not_the_overview(self):
        html = self.page()
        self.assertNotIn("Set up, and worth fixing", html)
        self.assertIn("Restaurant setup needs attention", html)
        self.assertGreater(html.index('id="branchProblems"'), html.index('id="p-branches"'))

    def test_the_page_knows_whether_it_is_an_owner_looking(self):
        """The two sections that change the shape of the business are the
        owner's. Hiding them is a courtesy — the endpoints refuse them anyway —
        but a control that will be refused should not be on the screen."""
        self.assertTrue(self.context_for(roles.RESTAURANT_OWNER).is_owner)

    def test_a_manager_is_told_they_are_not_the_owner(self):
        self.assertFalse(self.context_for(roles.RESTAURANT_MANAGER).is_owner)

    def test_the_owner_only_sections_are_marked_as_such(self):
        """`data-owner` is what the page removes for a manager. If the marker
        moves, a manager gets a Staff section whose every button 403s."""
        html = self.page()
        for section in ("branches", "staff"):
            with self.subTest(section=section):
                self.assertRegex(html, r'data-panel="%s"[^>]*data-owner="1"' % section)

    def test_owner_is_now_a_redirect_into_the_portal(self):
        """It stays because it is bookmarked — the same reason /pos2 stayed for
        a release after the POS pages collapsed."""
        self.as_user(_as(roles.RESTAURANT_OWNER))
        with self.assertRaises(frappe.Redirect):
            self.context(importlib.import_module("luuvcrm.www.owner"))
        self.assertEqual(frappe.local.flags.redirect_location, "/admin#branches")

    def test_the_old_owner_page_carries_no_logic_of_its_own(self):
        """Two screens that both know how to edit a branch is how they drift."""
        owner = importlib.import_module("luuvcrm.www.owner")
        self.assertFalse(hasattr(owner, "roles"),
                         "the redirect should not be gating or rendering anything")
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "owner.html"))
        self.assertLess(len(html), 500, "owner.html should be a stub")
        self.assertIn("/admin#branches", html)

    def test_the_apk_link_points_at_the_file_that_exists(self):
        """The old banner offered /files/app-release.apk. The APK is served from
        the app's own assets, and a download button that 404s is worse than no
        button — nobody reports it, they just decide the app is broken."""
        import pathlib

        html = self.page()
        self.assertNotIn("/files/app-release.apk", html)
        self.assertIn("/assets/luuvcrm/luuvcrm-print.apk", html)
        apk = pathlib.Path(frappe.get_app_path("luuvcrm", "public", "luuvcrm-print.apk"))
        self.assertTrue(apk.exists(), "the file the button offers must be in public/")

    def test_the_portal_does_not_pull_a_library_from_the_internet(self):
        """The launcher loaded lucide from unpkg on every open. A till on a
        restaurant's wifi should not need a CDN to draw its own icons, and a
        page that waits for one is a page that hangs when the line is bad."""
        self.assertNotIn("unpkg.com", self.page())

    def test_the_table_toggle_stays_on_its_table_row(self):
        """Seats/open chips share the name cell; otherwise the fifth grid
        child pushes Turn off onto a second row on a phone."""
        html = self.page()
        self.assertIn('.li.table-row{grid-template-columns:minmax(0,1fr) auto}', html)
        self.assertIn('<div class="li table-row"${off}>', html)
        self.assertIn('<div class="tags">${seats}${open}</div>', html)

    def test_menu_screens_identify_the_restaurant_not_the_menu_record(self):
        """Restaurant Menu names are internal card/version identifiers. Staff
        must see which restaurant they are changing, especially when one is
        unfortunately named something generic such as "New"."""
        admin = self.page()
        manager = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "manager.html"))
        self.assertIn("d.items.length + ' items · ' + d.restaurant", admin)
        self.assertIn("d.restaurant + ' · no live menu configured yet'", admin)
        self.assertNotIn("' items on ' + d.menu", admin)
        self.assertIn("(m.restaurant||'Restaurant')+' · '+m.items.length+' items'", manager)
        self.assertIn("'New item for '+(setData.restaurant||'this restaurant')", manager)
        self.assertNotIn("(m.menu||'Menu')+' · '", manager)

    def test_purchases_has_the_same_bom_editor_as_admin(self):
        """Purchasing staff should maintain recipes without opening Desk."""
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "purchases.html"))
        self.assertIn('data-tab="recipes"', html)
        self.assertIn("get_purchase_recipe_items", html)
        self.assertIn("save_purchase_recipe", html)
        self.assertIn("prepare_item_batch", html)


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
               "manager.html": 8000, "pos.html": 8000}
    # owner.html is off this list because it is a five-line redirect stub now:
    # its polling went into /admin with the rest of the screen.

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
                         # pollDecision: one approval's answer — 3 s, then 10 s,
                         # stopped at 1480 tries (four hours) or the answer.
                         "iv=setInterval(tick,3000)", "iv=setInterval(tick,10000)"],
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
