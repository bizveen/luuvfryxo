"""POS order cancellation: the direct cancel, the guard once paid, and the
manager-approved void."""

import json
import frappe
from frappe.tests.utils import FrappeTestCase
from luuvcrm import api
from luuvcrm.tests.utils import (ensure_setup, as_pos_user, form, make_item,
                                 make_pos_order, make_approval)


class TestPosCancel(FrappeTestCase):
    def setUp(self):
        as_pos_user()
        ensure_setup()
        make_item()

    def test_cancel_order_marks_cancelled(self):
        o = make_pos_order([{"item": "_LFT-TEST-ITEM", "qty": 1, "rate": 100}])
        form(order_name=o.name)
        api.cancel_order()
        o.reload()
        self.assertEqual(o.docstatus, 2)
        self.assertEqual(o.kitchen_status, "Cancelled")

    def test_cancel_blocked_after_payment(self):
        o = make_pos_order([{"item": "_LFT-TEST-ITEM", "qty": 1, "rate": 100}])
        o.db_set("pos_invoice", "SOME-INVOICE")   # bypass link validation on purpose
        form(order_name=o.name)
        with self.assertRaises(frappe.ValidationError):
            api.cancel_order()
        o.reload()
        self.assertEqual(o.docstatus, 0)           # untouched

    def test_void_approval_cancels_order(self):
        o = make_pos_order([{"item": "_LFT-TEST-ITEM", "qty": 1, "rate": 100}])
        appr = make_approval(approval_type="void", action="void", pos_order=o.name,
                             title="Void approval")
        form(name=appr.name, decision="approved")
        api.resolve_approval()
        o.reload()
        self.assertEqual(o.docstatus, 2)
        self.assertEqual(o.kitchen_status, "Cancelled")
        self.assertEqual(frappe.db.get_value("POS Approval", appr.name, "status"), "approved")
