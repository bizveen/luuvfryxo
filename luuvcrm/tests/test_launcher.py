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

from luuvcrm import activity, launcher, roles


def _user(case, tag, role):
    email = f"lf-test-launch-{tag}@example.com"
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": "Launch",
                        "send_welcome_email": 0, "roles": [{"role": role}]
                        }).insert(ignore_permissions=True)
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
