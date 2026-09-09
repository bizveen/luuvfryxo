"""The look, for a restaurant that is not this one.

The app named one client in its colours as well as its words: `#F15207`
hard-coded in twelve files, three static PWA manifests calling themselves Luuv
Fryxo, and — as it turned out — icon URLs that resolved to nothing at all.

The arrangement is the same one the rest of `settings.py` uses. What is new is
the delivery: a Jinja global, so `templates/includes/brand.html` can read the
brand on any page, including the ones with no `.py` of their own.
"""

import json
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, settings
from luuvcrm.overrides import branding
from luuvcrm.tests import fixtures as fx

PAGES = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))


class BrandTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        settings.forget()
        self.addCleanup(settings.forget)

    def set_brand(self, **values):
        doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
        previous = {k: doc.get(k) for k in values}
        for k, v in values.items():
            doc.set(k, v)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        settings.forget()

        def restore():
            d = frappe.get_single(settings.SETTINGS_DOCTYPE)
            for k, v in previous.items():
                d.set(k, v)
            d.flags.ignore_permissions = True
            d.save(ignore_permissions=True)
            # The change outlives the rollback: anything that commits mid-test —
            # the activity trail does, on every logged request — commits the
            # brand with it, and the site is left calling itself Sea Breeze Cafe.
            # The undo has to be committed the same way the change was.
            frappe.db.commit()
            settings.forget()

        self.addCleanup(restore)


class TestTheBrandReachesEveryPage(BrandTestCase):
    def test_the_jinja_global_is_registered(self):
        """Not `extend_website_context` — that hook does not exist in this
        Frappe, so the branding module it named had never once been called and
        every page kept its hard-coded orange."""
        self.assertIn("luuvcrm.overrides.branding.brand",
                      frappe.get_hooks("jinja").get("methods") or [])

    def test_a_template_can_ask_for_it(self):
        self.set_brand(brand_color="#0E7C86")
        self.assertEqual(frappe.render_template("{{ brand().brand_color }}", {}),
                         "#0E7C86")

    def test_the_include_renders_the_tokens(self):
        self.set_brand(brand_color="#0E7C86", brand_color_dark="#0A5C64")
        out = frappe.render_template('{% include "templates/includes/brand.html" %}', {})
        self.assertIn("--color-primary:#0E7C86", out)
        self.assertIn("--color-primary-hover:#0A5C64", out)
        self.assertIn('content="#0E7C86"', out, "the theme-color meta follows too")

    def test_every_screen_includes_it(self):
        """A page that forgets the include keeps the palette it shipped with,
        and one screen in the wrong colour is worse than none."""
        missing = []
        skip = {p.split("/")[-1] for p in fx.live_only_paths() if p.startswith("www/")}
        for page in sorted(PAGES.glob("*.html")):
            if page.name in skip:
                continue          # the clothing site's page, on the host only
            html = page.read_text()
            if "</head>" not in html:
                continue          # a redirect stub, not a screen
            if "templates/includes/brand.html" not in html:
                missing.append(page.name)
        self.assertEqual(missing, [], "these pages do not read the brand")

    def test_the_accent_is_not_hard_coded_past_the_include(self):
        """Each page still declares the original orange as its own default —
        that is deliberate, so it renders on a site with no settings — but the
        include comes last in <head> and wins."""
        html = (PAGES / "menu.html").read_text()
        head = html[:html.index("</head>")]
        self.assertLess(head.index("templates/includes/brand.html"),
                        head.index("</head>") if "</head>" in head else len(head))


class TestNoPageNamesOneClient(BrandTestCase):
    """The colours were swept first; the words were the half left over. 21 pages
    said "Luuv Fryxo" in their markup, the WhatsApp replies said it in their
    text, and the thermal print formats printed it — with one client's town and
    telephone number under it."""

    #: `PF_DEFAULTS` in pos.html names Print Format *records* the tills point at.
    #: Renaming those without renaming the records drops a till back to the
    #: built-in layout, so they are identifiers and stay.
    ALLOWED = {"pos.html": 2, "printing.py": 2}

    def occurrences(self, path):
        return path.read_text().count("Luuv Fryxo")

    def test_no_page_says_it_in_its_markup(self):
        named = {p.name: self.occurrences(p) for p in sorted(PAGES.glob("*.html"))
                 if self.occurrences(p) > self.ALLOWED.get(p.name, 0)}
        self.assertEqual(named, {}, "these pages still name one client")

    def test_no_message_says_it_either(self):
        """A WhatsApp reply, an OTP, a guide's welcome — all of them go out
        under the restaurant's name, and none of them is this restaurant's."""
        app = pathlib.Path(frappe.get_app_path("luuvcrm"))
        named = {}
        live_only = fx.live_only_paths()
        for py in sorted(app.rglob("*.py")):
            if "tests" in py.parts or py.name in ("settings.py", "hooks.py", "branding.py", "brand.py"):
                continue          # settings declares the default; the others only describe how it got here
            if py.relative_to(app).as_posix() in live_only:
                continue          # the clothing site's code, on the host only
            n = self.occurrences(py)
            if n > self.ALLOWED.get(py.name, 0):
                named[str(py.relative_to(app))] = n
        self.assertEqual(named, {}, "these modules still name one client")

    def test_the_pages_read_the_name_from_settings(self):
        self.set_brand(brand_name="Test Kitchen")
        self.assertEqual(branding.brand()["brand_name"], "Test Kitchen")
        ctx = frappe._dict()
        branding.update_website_context(ctx)
        self.assertEqual(ctx.brand_name, "Test Kitchen")
        self.assertEqual(ctx.app_name, "Test Kitchen")

    def test_a_passive_site_keeps_its_own_name(self):
        """This app is installed on a second site that is not a restaurant —
        its DocTypes live in a module this app owns. `update_website_context`
        and `boot_session` are bench-wide hooks, so without the switch that
        site's login page said "Luuv Fryxo". With `luuvcrm_passive` in its
        site_config, neither hook touches it."""
        from unittest.mock import patch

        self.set_brand(brand_name="Test Kitchen")
        with patch.dict(frappe.conf, {"luuvcrm_passive": 1}):
            self.assertTrue(settings.passive())
            ctx = frappe._dict(app_name="Zeloura")
            branding.update_website_context(ctx)
            self.assertEqual(ctx.app_name, "Zeloura")
            self.assertNotIn("brand_name", ctx)
            boot = frappe._dict(app_name="Zeloura", system_settings=frappe._dict(app_name="Zeloura"))
            branding.boot_session(boot)
            self.assertEqual(boot.app_name, "Zeloura")
        self.assertFalse(settings.passive())

    def test_the_context_hook_is_wired(self):
        """`update_website_context` is what puts `brand_name` in every page's
        context. The function existed and was never hooked — so every page that
        used it would have rendered an empty name."""
        self.assertIn("luuvcrm.overrides.branding.update_website_context",
                      frappe.get_hooks("update_website_context") or [])

    def test_the_printed_bill_carries_the_restaurant_and_not_a_town(self):
        """The guest check and the receipt had one client's name, town and
        telephone number set in the Print Format HTML."""
        from luuvcrm.api import printing
        self.set_brand(brand_name="Test Kitchen", outlet_line="Galle",
                       contact_phone="011 000 0000")
        for html in (printing.GUEST_CHECK_PF_HTML, printing.RECEIPT_PF_HTML):
            out = frappe.render_template(html[:html.index("</div>", html.index("r-header")) + 6], {})
            self.assertIn("Test Kitchen", out)
            self.assertIn("Galle", out)

    def test_the_till_prints_the_same_name_when_the_format_cannot_be_read(self):
        """pos.html carries an ESC/POS fallback the counter prints when the
        Print Format is unreachable. It said LUUV FRYXO / Dambulla in capitals."""
        html = (PAGES / "pos.html").read_text()
        self.assertNotIn("LUUV FRYXO", html)
        self.assertNotIn("Dambulla", html)
        self.assertIn("BRAND.toUpperCase()", html)


class TestTheIconsExist(BrandTestCase):
    """They did not. `/icon.svg` and `/manager-icon-{192,512}.png` were
    referenced by four pages and three manifests and every one of them 404'd —
    so the PWAs installed with no icon and the kitchen and waiter desktop
    notifications had none either."""

    def test_no_page_references_an_icon_that_does_not_exist(self):
        public = pathlib.Path(frappe.get_app_path("luuvcrm", "public"))
        broken = []
        for page in sorted(PAGES.glob("*.html")):
            html = page.read_text()
            for ref in ("/icon.svg", "/manager-icon-192.png", "/manager-icon-512.png"):
                if ref in html:
                    broken.append(f"{page.name} → {ref}")
        self.assertEqual(broken, [], "these resolve to 404")

    def test_the_defaults_point_at_files_that_are_really_there(self):
        public = pathlib.Path(frappe.get_app_path("luuvcrm", "public"))
        for key in ("brand_logo", "brand_icon_192", "brand_icon_512"):
            default = next(k.default for k in settings.KEYS if k.name == key)
            with self.subTest(key=key):
                self.assertTrue(default.startswith("/assets/luuvcrm/"))
                on_disk = public / default.replace("/assets/luuvcrm/", "")
                self.assertTrue(on_disk.exists(), f"{default} is not in public/")


class TestTheManifests(BrandTestCase):
    """Three static JSON files naming one client. Frappe templates only .html
    and .md under www/, so a .json there cannot read a setting — the manifests
    are built by an endpoint instead."""

    def test_it_carries_the_restaurant_s_own_name_and_colour(self):
        self.set_brand(brand_name="Sea Breeze Cafe", brand_short="Breeze",
                       brand_color="#0E7C86")
        body = self._manifest("manager")
        self.assertEqual(body["name"], "Sea Breeze Cafe Manager")
        self.assertEqual(body["short_name"], "Breeze Manager")
        self.assertEqual(body["theme_color"], "#0E7C86")

    def test_each_app_gets_its_own_scope(self):
        for app, scope in (("manager", "/manager"), ("purchases", "/purchases"),
                           ("cus", "/cus")):
            with self.subTest(app=app):
                self.assertEqual(self._manifest(app)["scope"], scope)

    def test_an_unknown_app_falls_back_rather_than_failing(self):
        """A manifest is fetched by the browser, not by our code; a 500 here is
        an install that silently does not happen."""
        self.assertEqual(self._manifest("nonsense")["scope"], "/cus")

    def test_it_is_served_as_a_manifest_and_not_a_download(self):
        """Content-Disposition: attachment would make the browser offer to save
        it, which is neither useful nor an installable PWA."""
        self._manifest("cus")
        self.assertEqual(self._captured.get("content_type"), "application/manifest+json")
        self.assertEqual(self._captured.get("display_content_as"), "inline")
        self.assertEqual(self._captured.get("type"), "download",
                         '"download" is the key for as_raw(); "raw" is a KeyError')

    def test_no_static_manifest_is_left_to_go_stale(self):
        self.assertEqual(sorted(p.name for p in PAGES.glob("*manifest*.json")), [])

    def test_the_pages_point_at_the_endpoint(self):
        for page, app in (("manager.html", "manager"), ("purchases.html", "purchases"),
                          ("cus.html", "cus")):
            html = (PAGES / page).read_text()
            with self.subTest(page=page):
                self.assertIn(f"luuvcrm.api.manifest?app={app}", html)

    def _manifest(self, app):
        previous = frappe.local.response
        frappe.local.response = frappe._dict()
        try:
            api.manifest(app=app)
            return json.loads(frappe.local.response["filecontent"])
        finally:
            captured = frappe.local.response
            frappe.local.response = previous
            self._captured = captured
