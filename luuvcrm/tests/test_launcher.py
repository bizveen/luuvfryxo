"""Where a person lands after signing in, and the way between apps once inside.

The login screen sent everybody to `/pos`: right for a cashier, a retyped URL for
a manager, and a 403 for a waiter or the kitchen. `/waiter` even served the login
form in place with no `?redirect`, so a waiter signing in from their own app was
sent to the till and refused. `/staff` replaces that default — tiles for whoever
moves between apps, straight through for whoever does not — and it is the start
URL of one installable staff PWA.
"""

import importlib

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import activity, branch, launcher, roles
from luuvcrm.tests.test_branch import _restaurant


def _user(case, tag, role):
    """A test user with no branch pinned and no branch chosen, on the way in and
    out. `branch.assign()` commits, so a pin from an earlier run outlives the
    rollback — see the fix in test_branch._user for what that cost."""
    email = f"lf-test-launch-{tag}@example.com"
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": "Launch",
                        "send_welcome_email": 0, "roles": [{"role": role}]
                        }).insert(ignore_permissions=True)
    branch.unassign(email)
    frappe.defaults.clear_user_default(branch.CHOSEN_KEY, email)
    case.addCleanup(branch.unassign, email)
    case.addCleanup(frappe.defaults.clear_user_default, branch.CHOSEN_KEY, email)
    return email


class LauncherTestCase(FrappeTestCase):
    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def land(self, **params):
        """Render /staff and report where it sent us: a path, or "chooser"."""
        previous_form, previous_flag = frappe.local.form_dict, frappe.local.flags.redirect_location
        frappe.local.form_dict = frappe._dict(**params)
        frappe.local.flags.redirect_location = None
        try:
            context = importlib.import_module("luuvcrm.www.staff").get_context(frappe._dict())
            return "chooser", context
        except frappe.Redirect:
            return frappe.local.flags.redirect_location, None
        finally:
            frappe.local.form_dict = previous_form
            frappe.local.flags.redirect_location = previous_flag

    def keys(self, email):
        return [a["key"] for a in launcher.apps_for(email)]


class TestWhoIsOfferedWhat(LauncherTestCase):
    def test_a_manager_is_offered_the_back_office_and_the_floor(self):
        keys = self.keys(_user(self, "mgr", roles.RESTAURANT_MANAGER))
        for key in ("admin", "manager", "purchases", "pos", "waiter", "kitchen"):
            self.assertIn(key, keys)

    def test_a_cashier_is_offered_the_floor_only(self):
        keys = self.keys(_user(self, "cash", roles.CASHIER))
        self.assertEqual(keys[0], "pos", "a cashier lands on the till")
        for key in ("admin", "manager", "purchases"):
            self.assertNotIn(key, keys)

    def test_the_kitchen_is_offered_the_kitchen(self):
        self.assertEqual(self.keys(_user(self, "kit", roles.KITCHEN_STAFF)), ["kitchen"])

    def test_a_guest_is_offered_nothing(self):
        self.assertEqual(launcher.apps_for("Guest"), [])

    def test_every_tile_is_a_page_that_exists(self):
        """A tile for a screen that is not there is a 404 with a nice label."""
        import pathlib

        www = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))
        for app in launcher.APPS:
            with self.subTest(app=app["key"]):
                self.assertTrue((www / (app["path"].strip("/") + ".py")).exists())


class TestWhereSigningInLands(LauncherTestCase):
    def test_a_guest_is_sent_to_sign_in_and_brought_back_here(self):
        self.as_user("Guest")
        where, _ = self.land()
        self.assertEqual(where, "/pos-login?redirect=/staff")

    def test_a_manager_is_shown_the_tiles(self):
        self.as_user(_user(self, "mgr2", roles.RESTAURANT_MANAGER))
        where, context = self.land()
        self.assertEqual(where, "chooser")
        self.assertIn("admin", [a["key"] for a in context.apps])

    def test_a_cashier_goes_straight_to_the_till(self):
        """Five tiles every morning is a wrong tap mid-rush."""
        self.as_user(_user(self, "cash2", roles.CASHIER))
        self.assertEqual(self.land()[0], "/pos")

    def test_a_waiter_goes_to_the_waiter_screen_not_the_till(self):
        """The bug this replaces: a waiter was sent to /pos and refused."""
        self.as_user(_user(self, "wait", roles.WAITER))
        self.assertEqual(self.land()[0], "/waiter")

    def test_but_the_switch_still_shows_a_cashier_the_tiles(self):
        """A cashier covering the pass needs a way to the kitchen screen."""
        self.as_user(_user(self, "cash3", roles.CASHIER))
        self.assertEqual(self.land(choose="1")[0], "chooser")

    def test_somebody_with_one_app_is_never_shown_a_single_tile(self):
        self.as_user(_user(self, "kit2", roles.KITCHEN_STAFF))
        self.assertEqual(self.land(choose="1")[0], "/kitchen")

    def test_the_login_form_lands_on_staff_by_default(self):
        # Not frappe.get_app_path: it scrubs every segment, turning the hyphen in
        # `pos-login` into an underscore and pointing at a file that is not there.
        import pathlib

        www = pathlib.Path(launcher.__file__).parent / "www"
        self.assertIn("|| '/staff'", (www / "pos-login.html").read_text())
        self.assertIn('"redirect", "/staff"', (www / "pos-login.py").read_text())


class TestTheStaffApp(LauncherTestCase):
    def test_its_manifest_starts_on_the_chooser_and_covers_every_app(self):
        """Scope `/`, or moving from /admin to /pos drops out of the installed
        window into a browser tab with an address bar."""
        import json

        from luuvcrm.api import brand

        brand.manifest(app="staff")
        body = json.loads(frappe.local.response["filecontent"])
        self.assertEqual(body["start_url"], "/staff")
        self.assertEqual(body["scope"], "/")
        self.assertEqual(body["display"], "standalone")

    def test_the_other_manifests_did_not_move(self):
        import json

        from luuvcrm.api import brand

        brand.manifest(app="manager")
        body = json.loads(frappe.local.response["filecontent"])
        self.assertEqual((body["start_url"], body["scope"]), ("/manager", "/manager"))

    def test_the_page_asks_for_it_and_reports_its_errors(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "staff.html"))
        self.assertIn("luuvcrm.api.manifest?app=staff", html)
        self.assertIn('data-app="staff"', html)
        self.assertIn("staff", activity.KNOWN_APPS)

    def test_every_app_has_a_way_back(self):
        for page in ("pos", "waiter", "kitchen", "manager", "purchases"):
            html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
            with self.subTest(page=page):
                self.assertIn("templates/includes/app_switch.html", html)
        admin = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "admin.html"))
        self.assertIn(launcher.CHOOSER, admin)


class TestTheRestaurantComesFirst(LauncherTestCase):
    """An owner or admin with more than one restaurant picks the restaurant
    before anything else — the choice every screen and every one of their bills
    then resolves through."""

    def test_on_one_restaurant_there_is_nothing_to_pick(self):
        if len(branch.all_restaurants()) != 1:
            self.skipTest("this site has more than one Restaurant")
        self.as_user(_user(self, "own-one", roles.RESTAURANT_OWNER))
        self.assertEqual(self.land()[1].branches, [])

    def test_an_owner_with_two_restaurants_picks_one_first(self):
        _restaurant(self, "LAUNCH-B")
        self.as_user(_user(self, "own-two", roles.RESTAURANT_OWNER))
        where, context = self.land()
        self.assertEqual(where, "chooser")
        self.assertGreaterEqual(len(context.branches), 2)

    def test_last_time_s_choice_is_marked(self):
        other = _restaurant(self, "LAUNCH-C")
        email = _user(self, "own-last", roles.RESTAURANT_OWNER)
        branch.select(other, email)
        self.as_user(email)
        self.assertEqual(self.land()[1].chosen, other)

    def test_a_manager_pinned_to_a_branch_is_not_asked(self):
        """Their branch is not a preference; `branch.select` would refuse the
        other one anyway."""
        other = _restaurant(self, "LAUNCH-D")
        email = _user(self, "mgr-pinned", roles.RESTAURANT_MANAGER)
        branch.assign(email, other)
        self.as_user(email)
        self.assertEqual(self.land()[1].branches, [])

    def test_the_restaurant_step_is_skipped_when_coming_from_the_switch(self):
        """The page renders both steps and shows one; `asked` is what it reads.
        Somebody moving between apps is not starting their day."""
        _restaurant(self, "LAUNCH-E")
        self.as_user(_user(self, "own-switch", roles.RESTAURANT_OWNER))
        self.assertTrue(self.land(choose="1")[1].asked)
        self.assertFalse(self.land()[1].asked)


class TestTilesAreForAPhoneOrTheInstalledApp(LauncherTestCase):
    def html(self):
        return frappe.read_file(frappe.get_app_path("luuvcrm", "www", "staff.html"))

    def test_at_a_desk_a_manager_goes_to_the_back_office(self):
        self.as_user(_user(self, "mgr-desk", roles.RESTAURANT_MANAGER))
        self.assertEqual(self.land()[1].desktop_home, "/admin")

    def test_the_page_knows_a_phone_and_the_installed_app_apart_from_a_desk(self):
        """Only the browser can tell, so the rule lives on the page. Pinned here
        so it cannot quietly become "always tiles" again."""
        html = self.html()
        self.assertIn("(display-mode: standalone)", html)
        self.assertIn("navigator.standalone", html)
        self.assertIn("(max-width: 820px)", html)
        self.assertIn("location.replace", html)

    def test_the_switch_still_shows_tiles_at_a_desk(self):
        """The early redirect is only for a fresh open with nothing to ask."""
        self.assertIn("{%- if not branches and not asked %}", self.html())

    def test_choosing_a_restaurant_goes_through_the_same_endpoint_as_admin(self):
        self.assertIn("luuvcrm.api.set_branch_choice", self.html())
        frappe.is_whitelisted(frappe.get_attr("luuvcrm.api.set_branch_choice"))


class TestWhereAPurchaserLands(LauncherTestCase):
    def test_a_purchaser_goes_straight_to_purchases(self):
        self.as_user(_user(self, "buy", roles.PURCHASER))
        self.assertEqual(self.land()[0], "/purchases")

    def test_a_cashier_who_also_buys_lands_on_the_till(self):
        """The morning needs the till; Purchases is one tap away on the switch."""
        email = _user(self, "buy-cash", roles.PURCHASER)
        frappe.get_doc("User", email).add_roles(roles.CASHIER)
        self.as_user(email)
        self.assertEqual(self.land()[0], "/pos")
        self.assertIn("purchases", [a["key"] for a in launcher.apps_for(email)])

    def test_a_cashier_is_not_offered_purchases(self):
        self.assertNotIn("purchases", self.keys(_user(self, "cash-nobuy", roles.CASHIER)))


class TestSigningOut(LauncherTestCase):
    """Frappe's /api/method/logout ends the session and answers with JSON, so the
    installed app — no address bar — was left on `{"message":"No App"}` and the
    button was reported as not working. sign_out lands on the login form."""

    STAFF_PAGES = ("pos", "waiter", "manager", "purchases", "admin", "staff")

    def sign_out(self, user):
        from unittest.mock import MagicMock

        self.as_user(user)
        previous = getattr(frappe.local, "login_manager", None)
        frappe.local.login_manager = MagicMock()
        self.addCleanup(setattr, frappe.local, "login_manager", previous)
        frappe.local.response = frappe._dict()
        launcher.sign_out()
        return frappe.local.login_manager.logout

    def test_it_ends_the_session_and_lands_on_the_login_form(self):
        logout = self.sign_out(_user(self, "out", roles.CASHIER))
        logout.assert_called_once()
        self.assertEqual(frappe.local.response["type"], "redirect")
        self.assertEqual(frappe.local.response["location"], "/pos-login")

    def test_tapping_it_again_is_not_an_error(self):
        logout = self.sign_out("Guest")
        logout.assert_not_called()
        self.assertEqual(frappe.local.response["location"], "/pos-login")

    def test_it_is_a_link_a_signed_out_browser_can_follow(self):
        method = launcher.sign_out
        self.assertIn(method, frappe.guest_methods)
        self.assertIn("GET", frappe.allowed_http_methods_for_whitelisted_func[method])

    def test_no_staff_page_links_to_the_json_logout_any_more(self):
        for page in self.STAFF_PAGES:
            html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", f"{page}.html"))
            with self.subTest(page=page):
                self.assertNotIn("/api/method/logout", html)
                self.assertIn("/api/method/luuvcrm.launcher.sign_out", html)

    def test_the_pos_webview_navigates_to_login_after_sign_out(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.html"))
        self.assertIn("fetch('/api/method/luuvcrm.launcher.sign_out'", html)
        self.assertIn("window.location.replace('/pos-login')", html)
        logout = html[html.index("async function doLogout"):html.index("function tick")]
        self.assertIn("await lfConfirm('Log out?'", logout)
        self.assertNotIn("window.confirm(", logout)
        self.assertNotIn("if(confirm(", logout)

    def test_the_waiter_webview_uses_an_in_page_logout_confirmation(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "waiter.html"))
        self.assertIn('id="logoutConfirm"', html)
        self.assertIn("fetch('/api/method/luuvcrm.launcher.sign_out'", html)
        self.assertIn("window.location.replace('/pos-login')", html)
        logout = html[html.index("function openLogoutConfirm"):html.index("// ─── WhatsApp")]
        self.assertNotIn("confirm(", logout)
