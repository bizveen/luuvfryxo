"""Manager approvals: creation, the duplicate-tap guard, and the purchase-bill
price overlay shown on the approve card."""

import json
import frappe
from frappe.tests.utils import FrappeTestCase
from luuvcrm import api
from luuvcrm.tests.utils import (ensure_setup, as_pos_user, form, make_item,
                                 make_pos_order, set_buying_price)


class TestApprovals(FrappeTestCase):
    def setUp(self):
        as_pos_user()
        ensure_setup()
        make_item()
        self.order = make_pos_order([{"item": "_LFT-TEST-ITEM", "qty": 2, "rate": 100}])

    def _remove_req(self, items):
        form(approval_type="edit", pos_order=self.order.name, title="Remove item approval",
             subhead="t", details_json="{}", flag_text="f", action="update_items",
             action_payload=json.dumps({"items": items}))
        return api.create_approval()

    def test_create_inserts_pending(self):
        r = self._remove_req([{"item": "A", "qty": 1}])
        self.assertTrue(r["ok"])
        self.assertEqual(frappe.db.get_value("POS Approval", r["name"], "status"), "pending")
        self.assertFalse(r.get("duplicate"))

    def test_identical_taps_dedupe(self):
        a = self._remove_req([{"item": "A", "qty": 1}])
        b = self._remove_req([{"item": "A", "qty": 1}])
        self.assertEqual(a["name"], b["name"])
        self.assertTrue(b.get("duplicate"))
        self.assertEqual(frappe.db.count("POS Approval",
                         {"pos_order": self.order.name, "status": "pending"}), 1)

    def test_different_payload_not_deduped(self):
        a = self._remove_req([{"item": "A", "qty": 1}])
        c = self._remove_req([{"item": "A", "qty": 2}])
        self.assertNotEqual(a["name"], c["name"])
        self.assertEqual(frappe.db.count("POS Approval",
                         {"pos_order": self.order.name, "status": "pending"}), 2)

    def test_resolved_one_does_not_block_new(self):
        a = self._remove_req([{"item": "A", "qty": 1}])
        frappe.db.set_value("POS Approval", a["name"], "status", "approved")
        b = self._remove_req([{"item": "A", "qty": 1}])
        self.assertNotEqual(a["name"], b["name"])
        self.assertFalse(b.get("duplicate"))

    def test_purchase_card_shows_price_delta(self):
        # price moved 100 -> 150; the approve card line must carry the overlay so
        # the "was Rs" indicator can render.
        set_buying_price("_LFT-TEST-ITEM", 100)
        form(vendor="Acme", note="", receipt_image="",
             lines=json.dumps([{"item": "_LFT-TEST-ITEM", "qty": 2, "price": 150, "unit": "Nos"}]))
        bill = api.create_purchase_bill()
        res = api.get_pending_approvals()
        card = next(a for a in res["approvals"]
                    if a["type"] == "bill" and a["title"] == "Acme")
        line = card["lines"][0]
        self.assertEqual(float(line["old_price"]), 100.0)
        self.assertEqual(float(line["price"]), 150.0)
        self.assertEqual(float(line["qty"]), 2.0)
        self.assertEqual(line["unit"], "Nos")
