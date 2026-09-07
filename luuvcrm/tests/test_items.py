"""The item master — everything the restaurant sells or buys, menu or not.

The menu is a selection; this is the list it selects from. The distinction is
not academic: most of what a restaurant buys (oil, gas, packaging) is never on a
menu, an add-on exists only to be attached to something else, and an item taken
off the menu for the season still has to be findable. `update_menu_item` refuses
anything that is not on the active menu — right for a menu screen, useless here.

Nothing deletes. Every invoice line, order line and purchase row links to an
Item, so retiring one is `disabled`, and the past keeps its names.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.api import menu as M
from luuvcrm.tests import fixtures as fx


class ItemTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.group = fx.item_group(self, "ITEM-TEST-GROUP")
        self._made = []
        self.addCleanup(self._drop)

    def _drop(self):
        for code in self._made:
            for dt, key in (("Item Price", "item_code"), ("Item", "name")):
                name = frappe.db.get_value(dt, {key: code}, "name")
                if name:
                    try:
                        frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
                    except Exception:
                        frappe.db.rollback()

    def make(self, name=None, **kwargs):
        name = name or "ITEM-%s" % frappe.generate_hash(length=6).upper()
        self._made.append(name)
        kwargs.setdefault("group", self.group)
        kwargs.setdefault("rate", 250)
        api.create_item(item_name=name, **kwargs)
        return name

    def row(self, code, **kwargs):
        listing = api.get_items(**kwargs)
        return next((i for i in listing["items"] if i["code"] == code), None)


class TestCreatingItems(ItemTestCase):
    def test_an_item_can_exist_without_a_menu(self):
        """Most of what a restaurant buys is never on a menu."""
        code = self.make()
        row = self.row(code)
        self.assertIsNotNone(row)
        self.assertFalse(row["on_menu"])
        self.assertEqual(row["rate"], 250)

    def test_a_stock_item_is_marked_as_one(self):
        code = self.make(is_stock_item=1, uom="Nos")
        self.assertEqual(self.row(code)["is_stock_item"], 1)

    def test_a_duplicate_name_is_refused(self):
        code = self.make()
        with self.assertRaises(frappe.ValidationError):
            api.create_item(item_name=code, rate=100, group=self.group)

    def test_a_category_that_does_not_exist_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.create_item(item_name="ITEM-NOWHERE", rate=100, group="Not A Category")

    def test_an_item_with_no_price_is_allowed(self):
        """A purchase-only item has a buying price, not a selling one."""
        code = self.make(rate=0)
        self.assertIsNotNone(self.row(code))


class TestEditingItems(ItemTestCase):
    def test_it_edits_something_that_is_not_on_the_menu(self):
        """The whole reason this module exists next to update_menu_item."""
        code = self.make()
        api.update_item(item_code=code, rate=400)
        self.assertEqual(self.row(code)["rate"], 400)

    def test_the_menu_editor_refuses_the_same_item(self):
        code = self.make()
        with self.assertRaises(frappe.ValidationError):
            api.update_menu_item(item_code=code, rate=400)

    def test_a_price_change_keeps_the_menu_row_in_step(self):
        """Two places carry a price — the POS price list, which everything
        resolves through, and the menu row's fallback. They must not drift."""
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        menu = M.active_menu_name(restaurant)
        if not menu:
            self.skipTest("no active menu on this site")
        code = self.make()
        doc = frappe.get_doc("Restaurant Menu", menu)
        M._append_menu_row(doc, code, 250)
        self.addCleanup(frappe.db.delete, "Restaurant Menu Item",
                        {"parent": menu, "item": code})

        api.update_item(item_code=code, rate=475)
        row_rate = frappe.db.get_value("Restaurant Menu Item",
                                       {"parent": menu, "item": code}, "rate")
        self.assertEqual(float(row_rate), 475)

    def test_a_zero_price_is_refused(self):
        code = self.make()
        with self.assertRaises(frappe.ValidationError):
            api.update_item(item_code=code, rate=0)

    def test_an_out_of_range_guide_commission_is_refused(self):
        code = self.make()
        with self.assertRaises(frappe.ValidationError):
            api.update_item(item_code=code, guide_pct=90)

    def test_tags_are_tidied(self):
        code = self.make()
        api.update_item(item_code=code, tags="Spicy, ,Spicy, Vegan")
        self.assertEqual(self.row(code)["tags"], "Spicy, Vegan")

    def test_an_item_that_does_not_exist_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.update_item(item_code="NO-SUCH-ITEM", rate=100)


class TestRetiringItems(ItemTestCase):
    def test_retiring_hides_it_from_the_default_listing(self):
        code = self.make()
        api.set_item_enabled(item_code=code, enabled=0)
        self.assertIsNone(self.row(code))
        self.assertIsNotNone(self.row(code, include_disabled=1))

    def test_nothing_is_ever_deleted(self):
        code = self.make()
        api.set_item_enabled(item_code=code, enabled=0)
        self.assertTrue(frappe.db.exists("Item", code),
                        "every past bill links to this item by name")

    def test_it_can_be_brought_back(self):
        code = self.make()
        api.set_item_enabled(item_code=code, enabled=0)
        api.set_item_enabled(item_code=code, enabled=1)
        self.assertIsNotNone(self.row(code))

    def test_something_on_the_menu_cannot_be_retired_from_here(self):
        """Otherwise the item vanishes from the till and the menu screen still
        lists it, and nobody can see why."""
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        menu = M.active_menu_name(restaurant)
        if not menu:
            self.skipTest("no active menu on this site")
        code = self.make()
        doc = frappe.get_doc("Restaurant Menu", menu)
        M._append_menu_row(doc, code, 250)
        self.addCleanup(frappe.db.delete, "Restaurant Menu Item",
                        {"parent": menu, "item": code})
        with self.assertRaises(frappe.ValidationError):
            api.set_item_enabled(item_code=code, enabled=0)


class TestTheListing(ItemTestCase):
    def test_it_says_which_items_are_on_the_menu(self):
        listing = api.get_items()
        self.assertIn("menu", listing)
        for item in listing["items"]:
            self.assertIn("on_menu", item)

    def test_it_has_a_ceiling(self):
        """A site that grows a supplier catalogue must not send it all to a
        phone."""
        listing = api.get_items()
        self.assertLessEqual(len(listing["items"]), listing["ceiling"])

    def test_search_narrows_it(self):
        code = self.make()
        listing = api.get_items(search=code)
        self.assertIn(code, [i["code"] for i in listing["items"]])
        self.assertNotIn("Walk In", [i["name"] for i in listing["items"]])

    def test_a_cashier_may_not_read_or_write_the_item_master(self):
        email = "lf-test-items-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Item Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        for call, kwargs in ((api.get_items, {}),
                             (api.create_item, {"item_name": "X", "rate": 1, "group": self.group}),
                             (api.update_item, {"item_code": "X", "rate": 1}),
                             (api.set_item_enabled, {"item_code": "X", "enabled": 0})):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.PermissionError):
                    call(**kwargs)
