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
        for page in sorted(PAGES.glob("*.html")):
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
