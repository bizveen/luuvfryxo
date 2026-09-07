"""Everything that changes from one restaurant to the next.

The app named one client in 25 places: "Luuv Fryxo" across 21 pages,
`luuvgrand.com` in the WhatsApp messages, a business number, a pickup town and
four Google Doc ids. None of it can be shipped to a second customer and none of
it belongs in a commit.

The arrangement is Laravel's, in Frappe's terms — `site_config.json` is the
`.env` (already outside the repo, one file per site, where Frappe keeps the
database password), `LuuvCRM Settings` is the database row the owner edits, and
the defaults in `settings.KEYS` are the config file. What these tests pin is the
part that is easy to get wrong: **each key has exactly one home**, so changing
it in the wrong place cannot silently do nothing.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import settings


class SettingsTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        settings.forget()
        self.addCleanup(settings.forget)

    def set_owner_value(self, key, value):
        doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
        previous = doc.get(key)
        doc.set(key, value)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        settings.forget()

        def restore():
            d = frappe.get_single(settings.SETTINGS_DOCTYPE)
            d.set(key, previous)
            d.flags.ignore_permissions = True
            d.save(ignore_permissions=True)
            settings.forget()

        self.addCleanup(restore)

    def set_operator_value(self, key, value):
        full = f"luuvcrm_{key}"
        previous = frappe.conf.get(full)
        frappe.conf[full] = value
        settings.forget()
        self.addCleanup(lambda: (frappe.conf.__setitem__(full, previous)
                                 if previous is not None else frappe.conf.pop(full, None),
                                 settings.forget()))


class TestEveryKeyHasOneHome(SettingsTestCase):
    def test_an_owner_key_comes_from_the_settings_single(self):
        self.set_owner_value("brand_name", "_LFT Second Restaurant")
        self.assertEqual(settings.get("brand_name"), "_LFT Second Restaurant")

    def test_an_operator_key_comes_from_site_config(self):
        self.set_operator_value("public_url", "https://second.example.com")
        self.assertEqual(settings.get("public_url"), "https://second.example.com")

    def test_an_owner_key_ignores_site_config(self):
        """One home each. If both were read, an owner editing the Desk field
        would watch their change do nothing and have no way to find out why."""
        self.set_operator_value("brand_name", "from site_config")
        self.assertNotEqual(settings.get("brand_name"), "from site_config")

    def test_an_operator_key_ignores_the_settings_single(self):
        self.assertEqual(
            [k.name for k in settings.KEYS if k.home == settings.OPERATOR
             and frappe.get_meta(settings.SETTINGS_DOCTYPE).get_field(k.name)],
            [], "an operator key must not have a Desk field to edit")

    def test_a_blank_falls_back_to_the_default(self):
        """A cleared field means "not set", not "empty string" — a blank brand
        name would otherwise render as nothing on every receipt."""
        self.set_owner_value("brand_name", "")
        self.assertEqual(settings.get("brand_name"), "Luuv Fryxo")

    def test_an_undeclared_key_is_a_mistake_not_a_none(self):
        """A typo would otherwise be a silent None all the way to the receipt."""
        with self.assertRaises(KeyError):
            settings.get("brand_nmae")


class TestItNeverTakesTheTillDown(SettingsTestCase):
    """A branding string must not be able to stop a sale."""

    def test_a_broken_settings_read_falls_back(self):
        original = settings._from_settings
        settings._from_settings = lambda name: (_ for _ in ()).throw(RuntimeError("no table"))
        self.addCleanup(setattr, settings, "_from_settings", original)
        self.assertEqual(settings.get("brand_name"), "Luuv Fryxo")

    def test_a_missing_settings_doctype_falls_back(self):
        original = frappe.db.exists
        frappe.db.exists = lambda dt, *a, **k: (
            None if dt == "DocType" and a and a[0] == settings.SETTINGS_DOCTYPE
            else original(dt, *a, **k))
        self.addCleanup(setattr, frappe.db, "exists", original)
        settings.forget()
        self.assertEqual(settings.get("pickup_outlet"), "Kurunegala")


class TestTheLinksThatOutliveADeploy(SettingsTestCase):
    """Every one of these ends up in a WhatsApp message in somebody's phone."""

    def test_url_is_built_from_the_setting(self):
        self.set_operator_value("public_url", "https://second.example.com")
        self.assertEqual(settings.url("/cus"), "https://second.example.com/cus")

    def test_a_trailing_slash_does_not_double(self):
        self.set_operator_value("public_url", "https://second.example.com/")
        self.assertEqual(settings.url("/cus"), "https://second.example.com/cus")

    def test_no_client_domain_is_left_hard_coded(self):
        """The check that stops the next one being pasted back in."""
        import subprocess
        hits = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--exclude-dir=graphify-out",
             "--exclude-dir=tests", "luuvgrand.com", frappe.get_app_path("luuvcrm")],
            capture_output=True, text=True).stdout.strip().splitlines()
        # settings.py is the one place allowed to name the first client: that is
        # where its default lives. Anywhere else is a message with a domain
        # baked into it.
        offenders = [h for h in hits if "/settings.py:" not in h]
        self.assertEqual(offenders, [], "a client domain is still built into a message")


class TestTheOwnerCanReachTheirOwnSettings(SettingsTestCase):
    def test_the_single_exists_with_a_field_per_owner_key(self):
        meta = frappe.get_meta(settings.SETTINGS_DOCTYPE)
        for key in [k for k in settings.KEYS if k.home == settings.OWNER]:
            with self.subTest(key=key.name):
                self.assertTrue(meta.get_field(key.name))

    def test_an_owner_may_edit_it_without_a_developer(self):
        from luuvcrm import roles
        perms = frappe.get_all("Custom DocPerm",
                               filters={"parent": settings.SETTINGS_DOCTYPE},
                               fields=["role", "write"])
        perms += frappe.get_all("DocPerm", filters={"parent": settings.SETTINGS_DOCTYPE},
                                fields=["role", "write"])
        writers = {p.role for p in perms if p.write}
        self.assertIn(roles.RESTAURANT_OWNER, writers)

    def test_describe_names_every_key_and_where_it_lives(self):
        rows = settings.describe()
        self.assertEqual(len(rows), len(settings.KEYS))
        for row in rows:
            self.assertIn(row["home"], ("site_config.json", settings.SETTINGS_DOCTYPE))

    def test_a_secret_is_not_printed_once_it_is_set(self):
        """The Google Doc ids are capabilities — the docs are shared 'anyone
        with the link'. `describe()` is run on a shared screen."""
        self.set_operator_value("print_template_docs", {"kot": "real-id"})
        row = next(r for r in settings.describe() if r["key"] == "print_template_docs")
        self.assertEqual(row["value"], "(set)")
