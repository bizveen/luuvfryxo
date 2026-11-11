"""Native ERPNext stock movements behind purchases and kitchen BOMs."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext.stock.utils import get_stock_balance

from luuvcrm import api, branch
from luuvcrm.api import pos_inventory
from luuvcrm.tests import fixtures as fx


class InventoryTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.restaurant = branch.current() or branch.viewing()
        self.assertTrue(self.restaurant, "inventory tests need a Restaurant")
        self.old_warehouse = frappe.db.get_value("Restaurant", self.restaurant, "warehouse")
        self.warehouse = self.old_warehouse or frappe.db.get_value(
            "Warehouse", {"is_group": 0, "disabled": 0}, "name"
        )
        self.assertTrue(self.warehouse, "inventory tests need a leaf Warehouse")
        frappe.db.set_value("Restaurant", self.restaurant, "warehouse", self.warehouse)
        self.company = frappe.db.get_value("Warehouse", self.warehouse, "company")

        self.menu_item = fx.item(self, "INV-MEAL")
        fx.item_price(self, self.menu_item, 1000)
        self.raw_item = self._stock_item("INV-RAW", "Kg")
        self.stock_items = [self.raw_item]
        self.entries = []
        self.orders = []
        self.bills = []
        self.boms = []
        self.addCleanup(self._cleanup_inventory)

    def _stock_item(self, suffix, uom):
        code = "_LFT-" + suffix
        if frappe.db.exists("Item", code):
            frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
        frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": code,
                "item_name": code,
                "item_group": fx.item_group(self),
                "stock_uom": uom,
                "is_stock_item": 1,
                "is_purchase_item": 1,
            }
        ).insert(ignore_permissions=True)
        return code

    def _cleanup_inventory(self):
        frappe.set_user("Administrator")
        # Discover entries by their audit links as well; a failed assertion may
        # happen before the test had a chance to remember the returned name.
        refs = set(self.entries)
        if frappe.db.has_column("Stock Entry", "luuv_pos_order") and self.orders:
            refs.update(
                frappe.get_all(
                    "Stock Entry",
                    filters={"luuv_pos_order": ["in", self.orders]},
                    pluck="name",
                )
            )
        if frappe.db.has_column("Stock Entry", "luuv_purchase_bill") and self.bills:
            refs.update(
                frappe.get_all(
                    "Stock Entry",
                    filters={"luuv_purchase_bill": ["in", self.bills]},
                    pluck="name",
                )
            )
        # Reverse the movement before its source receipt. Cancelling a receipt
        # first would make the later issue temporarily negative and ERPNext
        # correctly refuses that cleanup order.
        entry_rows = frappe.get_all(
            "Stock Entry",
            filters={"name": ["in", list(refs)]} if refs else {"name": ""},
            fields=["name", "purpose", "creation"],
            order_by="creation desc",
        )
        entry_rows.sort(key=lambda row: 0 if row.purpose == "Material Issue" else 1)
        for row in entry_rows:
            name = row.name
            if not frappe.db.exists("Stock Entry", name):
                continue
            doc = frappe.get_doc("Stock Entry", name)
            if doc.docstatus == 1:
                doc.flags.ignore_permissions = True
                doc.cancel()
            frappe.delete_doc("Stock Entry", name, force=True, ignore_permissions=True)
        for name in self.orders:
            if frappe.db.exists("POS Order", name):
                frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
        for name in self.bills:
            for approval in frappe.get_all(
                "POS Approval", filters={"action": "purchase_bill"}, pluck="name"
            ):
                payload = json.loads(
                    frappe.db.get_value("POS Approval", approval, "action_payload") or "{}"
                )
                if payload.get("bill") == name:
                    frappe.delete_doc(
                        "POS Approval", approval, force=True, ignore_permissions=True
                    )
            if frappe.db.exists("Purchase Bill", name):
                frappe.delete_doc("Purchase Bill", name, force=True, ignore_permissions=True)
        bom_names = set(self.boms)
        bom_names.update(
            frappe.get_all("BOM", filters={"item": self.menu_item}, pluck="name")
        )
        for name in bom_names:
            if frappe.db.exists("BOM", name):
                doc = frappe.get_doc("BOM", name)
                if doc.docstatus == 1:
                    doc.flags.ignore_permissions = True
                    doc.cancel()
                frappe.delete_doc("BOM", name, force=True, ignore_permissions=True)
        frappe.db.set_value("Restaurant", self.restaurant, "warehouse", self.old_warehouse)
        for item_code in reversed(self.stock_items):
            if frappe.db.exists("Item", item_code):
                frappe.delete_doc("Item", item_code, force=True, ignore_permissions=True)

    def call(self, endpoint, **kwargs):
        old = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint(**kwargs) if endpoint in (api.get_menu_recipe, api.save_menu_recipe) else endpoint()
        finally:
            frappe.local.form_dict = old

    def receive(self, qty=10, rate=100):
        entry = api._new_stock_entry(
            "Material Receipt", self.company, self.warehouse, "inventory test opening"
        )
        entry.append(
            "items",
            {
                "item_code": self.raw_item,
                "t_warehouse": self.warehouse,
                "qty": qty,
                "uom": "Kg",
                "conversion_factor": 1,
                "basic_rate": rate,
                "set_basic_rate_manually": 1,
            },
        )
        api._submit(entry)
        self.entries.append(entry.name)
        return entry

    def recipe(self, qty=0.25):
        result = api.save_menu_recipe(
            item_code=self.menu_item,
            ingredients=json.dumps([{"item": self.raw_item, "qty": qty}]),
            restaurant=self.restaurant,
        )
        self.boms.append(result["bom"])
        return result

    def balance(self):
        return float(get_stock_balance(self.raw_item, self.warehouse) or 0)


class TestMenuBOM(InventoryTestCase):
    def test_recipe_is_a_submitted_default_native_bom(self):
        result = self.recipe(0.2)
        bom = frappe.get_doc("BOM", result["bom"])
        self.assertEqual(bom.docstatus, 1)
        self.assertEqual(bom.item, self.menu_item)
        self.assertEqual(int(bom.is_default), 1)
        self.assertEqual(bom.items[0].item_code, self.raw_item)
        self.assertEqual(float(bom.items[0].qty), 0.2)

    def test_kitchen_fire_issues_the_bom_once(self):
        self.receive(10)
        self.recipe(0.25)
        before = self.balance()
        result = self.call(
            pos_inventory.send_to_kitchen,
            items=json.dumps([{"item": self.menu_item, "qty": 2}]),
            order_type="Dine In",
        )
        self.orders.append(result["name"])
        after = self.balance()
        self.assertAlmostEqual(before - after, 0.5, places=6)

        again = api.consume_order_stock(result["name"])
        self.assertIsNone(again["entry"])
        self.assertAlmostEqual(self.balance(), after, places=6)

    def test_ordinary_pos_does_not_consume_a_configured_recipe(self):
        self.recipe(0.25)
        result = self.call(api.send_to_kitchen,
                           items=json.dumps([{"item": self.menu_item, "qty": 2}]),
                           order_type="Take Away")
        self.orders.append(result["name"])
        self.assertEqual(api.consume_order_stock(result["name"])["skipped"], "ordinary_pos")
        self.assertFalse(frappe.db.exists("Stock Entry", {"luuv_pos_order": result["name"]}))

    def test_non_stock_item_without_a_bom_keeps_working(self):
        result = self.call(
            api.send_to_kitchen,
            items=json.dumps([{"item": self.menu_item, "qty": 1}]),
            order_type="Dine In",
        )
        self.orders.append(result["name"])
        self.assertFalse(
            frappe.db.exists("Stock Entry", {"luuv_pos_order": result["name"]})
        )

    def test_purchaser_can_add_and_update_a_bom_from_the_purchases_app(self):
        email = "lf-test-bom-purchaser@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "BOM Purchaser",
                    "send_welcome_email": 0,
                    "roles": [{"role": "Purchaser"}],
                }
            ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        try:
            result = api.save_purchase_recipe(
                item_code=self.menu_item,
                ingredients=json.dumps([{"item": self.raw_item, "qty": 0.3}]),
                restaurant=self.restaurant,
            )
            self.boms.append(result["bom"])
            self.assertEqual(result["ingredients"][0]["item"], self.raw_item)
            listed = api.get_purchase_recipe_items(restaurant=self.restaurant)
            row = next(r for r in listed["items"] if r["item"] == self.menu_item)
            self.assertEqual(row["bom"], result["bom"])
        finally:
            frappe.set_user(previous)

    def test_prepared_batch_is_stock_and_sale_consumes_it_without_reexploding(self):
        prepared = self._stock_item("INV-PREPARED", "Kg")
        self.stock_items.append(prepared)
        self.receive(10)

        prep_recipe = api.save_prepared_recipe(
            item_code=prepared,
            ingredients=json.dumps([{"item": self.raw_item, "qty": 0.5}]),
            restaurant=self.restaurant,
        )
        self.boms.append(prep_recipe["bom"])
        made = api.prepare_item_batch(
            item_code=prepared, qty=4, restaurant=self.restaurant
        )
        self.entries.append(made["entry"])
        self.assertAlmostEqual(self.balance(), 8, places=6)
        self.assertAlmostEqual(
            float(get_stock_balance(prepared, self.warehouse) or 0), 4, places=6
        )

        meal_recipe = api.save_menu_recipe(
            item_code=self.menu_item,
            ingredients=json.dumps(
                [
                    {"item": prepared, "qty": 0.25},
                    {"item": self.raw_item, "qty": 0.10},
                ]
            ),
            restaurant=self.restaurant,
        )
        self.boms.append(meal_recipe["bom"])
        result = self.call(
            pos_inventory.send_to_kitchen,
            items=json.dumps([{"item": self.menu_item, "qty": 2}]),
            order_type="Dine In",
        )
        self.orders.append(result["name"])

        # The final meal takes the prepared chicken/naan from the fridge plus
        # its direct raw ingredient.  It must not expand the prepared item's
        # BOM and charge its original raw material a second time.
        self.assertAlmostEqual(
            float(get_stock_balance(prepared, self.warehouse) or 0), 3.5, places=6
        )
        self.assertAlmostEqual(self.balance(), 7.8, places=6)


class TestPurchaseStockReceipt(InventoryTestCase):
    def test_approval_receives_stock_once(self):
        before = self.balance()
        result = self.call(
            api.create_purchase_bill,
            vendor="_LFT-Inventory Vendor",
            lines=json.dumps([{"item": self.raw_item, "qty": 3, "price": 400, "unit": "Kg"}]),
        )
        self.bills.append(result["name"])
        api._decide_purchase_bill(result["name"], "approved")
        bill = frappe.get_doc("Purchase Bill", result["name"])
        self.entries.append(bill.stock_entry)
        self.assertAlmostEqual(self.balance() - before, 3, places=6)
        self.assertEqual(
            frappe.db.get_value("Stock Entry", bill.stock_entry, "luuv_purchase_bill"),
            bill.name,
        )

        api._decide_purchase_bill(result["name"], "approved")
        self.assertAlmostEqual(self.balance() - before, 3, places=6)

    def test_approval_accepts_fractional_nos(self):
        item = self._stock_item("INV-FRACTIONAL-NOS", "Nos")
        self.stock_items.append(item)
        before = float(get_stock_balance(item, self.warehouse) or 0)
        result = self.call(
            api.create_purchase_bill,
            vendor="_LFT-Fractional Nos Vendor",
            lines=json.dumps(
                [{"item": item, "qty": 0.5, "price": 400, "unit": "Nos"}]
            ),
        )
        self.bills.append(result["name"])

        api._decide_purchase_bill(result["name"], "approved")

        bill = frappe.get_doc("Purchase Bill", result["name"])
        self.entries.append(bill.stock_entry)
        self.assertAlmostEqual(
            float(get_stock_balance(item, self.warehouse) or 0) - before,
            0.5,
            places=6,
        )

    def test_an_expense_category_on_a_mixed_bill_does_not_receive_stock(self):
        before = self.balance()
        result = self.call(
            api.create_purchase_bill,
            vendor="_LFT-Electricity Vendor",
            lines=json.dumps([{"name": "Electricity", "category": "Electricity",
                               "qty": 1, "price": 5000, "unit": "Bill"}]),
        )
        self.bills.append(result["name"])
        api._decide_purchase_bill(result["name"], "approved")
        bill = frappe.get_doc("Purchase Bill", result["name"])
        self.assertFalse(bill.stock_entry)
        self.assertAlmostEqual(self.balance(), before, places=6)
