"""Purchase bills: creation snapshot, the price-book update on approve/reject,
the zero-price skip, and the separation-of-duties block."""

import json
import frappe
from frappe.tests.utils import FrappeTestCase
from luuvcrm import api
from luuvcrm.tests.utils import (ensure_setup, as_pos_user, form, make_item,
                                 make_purchase_bill, set_buying_price, get_buying_price)


class TestPurchases(FrappeTestCase):
    def setUp(self):
        as_pos_user()
        ensure_setup()
        make_item()

    def test_create_bill_snapshots_old_price_and_routes_approval(self):
        set_buying_price("_LFT-TEST-ITEM", 100)
        form(vendor="Acme", note="", receipt_image="",
             lines=json.dumps([{"item": "_LFT-TEST-ITEM", "qty": 2, "price": 150, "unit": "Nos"}]))
        r = api.create_purchase_bill()
        self.assertTrue(r["ok"])
        self.assertEqual(r["total"], 300)
        bill = frappe.get_doc("Purchase Bill", r["name"])
        self.assertEqual(float(bill.items[0].old_price), 100.0)   # snapshot at creation
        self.assertEqual(float(bill.items[0].price), 150.0)
        # a matching POS Approval was routed to the manager
        self.assertTrue(frappe.db.exists("POS Approval",
                        {"action": "purchase_bill", "status": "pending"}))

    def test_approve_updates_price_book(self):
        set_buying_price("_LFT-TEST-ITEM", 100)
        bill = make_purchase_bill([{"item": "_LFT-TEST-ITEM", "qty": 2,
                                    "price": 150, "old_price": 100}])
        api._decide_purchase_bill(bill.name, "approved")
        self.assertEqual(get_buying_price("_LFT-TEST-ITEM"), 150.0)
        self.assertEqual(frappe.db.get_value("Purchase Bill", bill.name, "status"), "approved")

    def test_reject_leaves_price_book_untouched(self):
        set_buying_price("_LFT-TEST-ITEM", 100)
        bill = make_purchase_bill([{"item": "_LFT-TEST-ITEM", "qty": 2,
                                    "price": 150, "old_price": 100}])
        api._decide_purchase_bill(bill.name, "rejected")
        self.assertEqual(get_buying_price("_LFT-TEST-ITEM"), 100.0)
        self.assertEqual(frappe.db.get_value("Purchase Bill", bill.name, "status"), "rejected")

    def test_zero_price_line_is_skipped(self):
        # A line entered with no price must NOT overwrite the book with 0 — it is
        # skipped, so the previous price stays. (Guards the 'old amount stayed' case.)
        set_buying_price("_LFT-TEST-ITEM", 100)
        bill = make_purchase_bill([{"item": "_LFT-TEST-ITEM", "qty": 2,
                                    "price": 0, "old_price": 100}])
        api._decide_purchase_bill(bill.name, "approved")
        self.assertEqual(get_buying_price("_LFT-TEST-ITEM"), 100.0)

    def test_requester_cannot_approve_own_bill(self):
        bill = make_purchase_bill([{"item": "_LFT-TEST-ITEM", "qty": 1,
                                    "price": 50, "old_price": 40}],
                                  requested_by=frappe.session.user)
        form(name=bill.name, decision="approved")
        with self.assertRaises(frappe.PermissionError):
            api.resolve_purchase_bill()
