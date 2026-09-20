"""Tour Company: the service companies a rate card is made for.

Not the ERPNext Company — that is the restaurant. The rules a saved company must
keep (a name, a discount in range, at least one item) live on the DocType so a
row typed into the Desk obeys them the same as one saved from the Manager app.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import roles, setup_doctype
from luuvcrm.zeloura.doctype.tour_company.tour_company import parse_items


class TourCompanyTestCase(FrappeTestCase):
    def make(self, name="_LFT Blue Wave", discount=10, items=("K1", "K2"), **kw):
        doc = frappe.get_doc({"doctype": "Tour Company", "company_name": name,
                              "discount_percent": discount,
                              "items": json.dumps(list(items)), **kw})
        doc.insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Tour Company", doc.name,
                        force=True, ignore_permissions=True)
        return doc


class TestTheDocType(TourCompanyTestCase):
    def test_it_ships_with_the_app_and_is_on_the_roll_call(self):
        self.assertTrue(frappe.db.exists("DocType", "Tour Company"))
        self.assertIn("Tour Company", setup_doctype.EXPECTED_DOCTYPES)
        self.assertEqual(frappe.db.get_value("DocType", "Tour Company", "custom"), 0)

    def test_it_is_named_by_its_company_name(self):
        self.assertEqual(self.make("_LFT Acme").name, "_LFT Acme")

    def test_a_name_cannot_be_used_twice(self):
        self.make("_LFT Acme")
        with self.assertRaises(frappe.DuplicateEntryError):
            frappe.get_doc({"doctype": "Tour Company", "company_name": "_LFT Acme",
                            "discount_percent": 5, "items": '["K1"]'}).insert(ignore_permissions=True)


class TestWhatItRefuses(TourCompanyTestCase):
    def test_a_discount_outside_zero_to_fifty(self):
        for bad in (-1, 50.5, 100):
            with self.subTest(discount=bad):
                with self.assertRaises(frappe.ValidationError):
                    self.make(discount=bad)

    def test_the_limits_themselves_are_fine(self):
        self.make("_LFT Zero", discount=0)
        self.make("_LFT Fifty", discount=50)

    def test_no_items(self):
        for none in ([], ["", "  "]):
            with self.subTest(items=none):
                with self.assertRaises(frappe.ValidationError):
                    self.make(items=none)

    def test_no_name(self):
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({"doctype": "Tour Company", "company_name": "   ",
                            "discount_percent": 5, "items": '["K1"]'}).insert(ignore_permissions=True)

    def test_a_name_that_is_too_long(self):
        with self.assertRaises(frappe.ValidationError):
            self.make("x" * 81)


class TestWhatItTidies(TourCompanyTestCase):
    def test_spaces_in_the_name_are_collapsed(self):
        self.assertEqual(self.make("  _LFT   Blue    Wave ").name, "_LFT Blue Wave")

    def test_repeated_and_blank_item_codes_are_dropped_in_order(self):
        doc = self.make(items=("K2", "K1", "K2", " ", "B1"))
        self.assertEqual(json.loads(doc.items), ["K2", "K1", "B1"])

    def test_parse_items_takes_a_string_or_a_list_and_survives_rubbish(self):
        self.assertEqual(parse_items('["A","B"]'), ["A", "B"])
        self.assertEqual(parse_items(["A", "A"]), ["A"])
        for rubbish in (None, "", "not json", '{"a":1}', 5):
            self.assertEqual(parse_items(rubbish), [])


class TestWhoMayDoWhat(TourCompanyTestCase):
    def perms(self, role):
        return {p.role: p for p in frappe.get_meta("Tour Company").permissions}.get(role)

    def test_a_manager_and_an_owner_make_and_change_them(self):
        for role in (roles.RESTAURANT_MANAGER, roles.RESTAURANT_OWNER):
            with self.subTest(role=role):
                p = self.perms(role)
                self.assertTrue(p and p.read and p.create and p.write)

    def test_the_till_only_reads_them(self):
        p = self.perms(roles.CASHIER)
        self.assertTrue(p and p.read)
        self.assertFalse(p.create or p.write or p.delete)

    def test_a_waiter_sees_nothing(self):
        self.assertIsNone(self.perms(roles.WAITER))

    def test_nobody_but_system_manager_deletes(self):
        for role in (roles.RESTAURANT_MANAGER, roles.RESTAURANT_OWNER, roles.CASHIER):
            self.assertFalse(getattr(self.perms(role), "delete", 0), role)
