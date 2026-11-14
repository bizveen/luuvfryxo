"""Exercise actual payment endpoint bodies with isolated document/provider doubles.

No Frappe installation, database, queue, or SMS request is used. These tests
cover hook placement and metadata handoff; invoice accounting and durable SMS
deduplication remain the responsibilities of their own integration tests.
"""

import ast
from datetime import datetime
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "luuvcrm/api/payments.py"


class PaymentError(Exception):
    pass


class Document(SimpleNamespace):
    def __init__(self, harness, **values):
        super().__init__(**values)
        self._harness = harness
        self.flags = SimpleNamespace()
        self.docstatus = values.get("docstatus", 0)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def update(self, values):
        vars(self).update(values)

    def append(self, field, values):
        getattr(self, field).append(Document(self._harness, **values))

    def insert(self):
        if not self.get("name"):
            prefix = "INV" if self.doctype == "POS Invoice" else "ORDER"
            self.name = f"{prefix}-{len(self._harness.inserted) + 1}"
        self._harness.inserted.append(self)
        self._harness.documents[self.name] = self
        self._harness.events.append(("insert", self.doctype))
        return self

    def submit(self):
        self._harness.events.append(("submit", self.doctype))
        if self._harness.fail_submit:
            raise PaymentError("Invoice submission failed")
        self.docstatus = 1

    def db_set(self, key, value):
        setattr(self, key, value)

    def reload(self):
        return self


class PaymentHarness:
    def __init__(self):
        self.documents = {}
        self.inserted = []
        self.events = []
        self.reads = []
        self.fail_submit = False
        self.prepared = {}
        self.prepare = Mock(side_effect=self._prepare)
        self.queue = Mock(side_effect=self._queue)
        self.require_pos = Mock()
        self.require_access = Mock()
        self.profile = Document(self, name="Test POS", company="Test Co", currency="LKR",
                                selling_price_list="Test Selling", warehouse="Test Warehouse")
        self.frappe = SimpleNamespace(
            local=SimpleNamespace(form_dict={}), session=SimpleNamespace(user="test-cashier"),
            get_doc=self.get_doc, get_value=lambda *args: "Test Cashier",
            db=SimpleNamespace(get_value=lambda doctype, *args, **kwargs:
                               "Test Item" if doctype == "Item" else None),
            parse_json=json.loads, throw=self.throw,
        )
        self.modules = {}
        for name, values in {
            "luuvcrm.api.pos_customers": dict(prepare_pos_customer=self.prepare,
                                               queue_install_invite=self.queue),
            "luuvcrm.api.pos_inventory": dict(FIELD="inventory_mode", creation_mode=lambda: 0),
            "luuvcrm.api.inventory": dict(consume_order_stock=Mock()),
            "luuvcrm.api.loyalty": dict(finalize_reward_redemption=Mock(), _process_cashback=Mock()),
            "luuvcrm.api.offers": dict(finalize_offer_redemption=Mock()),
        }.items():
            module = ModuleType(name)
            vars(module).update(values)
            self.modules[name] = module

        noop = lambda *args, **kwargs: None
        self.namespace = dict(
            frappe=self.frappe, _=lambda text: text,
            now_datetime=lambda: datetime(2026, 11, 14, 12),
            roles=SimpleNamespace(require_pos=self.require_pos),
            branch=SimpleNamespace(require_access=self.require_access),
            activity=SimpleNamespace(discount_flag=lambda *args: ("", ""), log_activity=Mock()),
            GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE=False,
            _active_pos_profile_name=lambda requested: self.profile.name,
            _ensure_order_type_field=noop, _ensure_open_shift=noop,
            _server_item_rate=lambda code: 100,
            _item_is_takeaway=lambda data, kind: False,
            _clean_item_note=lambda note: note or "",
            _service_charge_on=lambda subtotal: (0, 0),
            _parse_guide_redeem=lambda raw, total: (None, 0),
            _tour_stamp=lambda raw: {}, _invoice_branch_fields=lambda profile: {},
            _invoice_doctype_for=lambda name: "POS Invoice",
            _append_service_charge_tax=noop, _set_cash_tender=noop,
            _ensure_item_notes_field=noop, _apply_takeaway_service_charge=noop,
            _link_invoice_to_order=noop, _finalize_guide_redeem=noop,
            _credit_guide_cashback=noop, _require_cus_call_released=noop,
            _get_service_charge_rate=lambda: 0,
            _order_fully_paid=lambda order: bool(order.items) and all(
                line.get("paid_qty", 0) >= line.qty for line in order.items),
        )
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"place_order", "process_payment", "pay_split"}]
        for node in functions:
            node.decorator_list = []
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                     str(SOURCE), "exec"), self.namespace)

    @staticmethod
    def throw(message):
        raise PaymentError(message)

    def _prepare(self, data):
        self.events.append(("prepare_customer", dict(data)))
        return dict(self.prepared)

    def _queue(self, name):
        order = self.documents[name]
        invoice = self.documents[order.pos_invoice]
        if invoice.docstatus != 1:
            raise AssertionError("Invitation queued before invoice submission")
        self.events.append(("queue", name))
        return True

    def get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            return Document(self, **doctype)
        self.reads.append((doctype, name, kwargs))
        return self.profile if doctype == "POS Profile" else self.documents[name]

    def order(self, qty=2, **overrides):
        values = dict(doctype="POS Order", name="ORDER-EXISTING", mobile="0770000010",
                      customer_name="Test Customer", pos_invoice="", docstatus=0,
                      kitchen_status="Ready", grand_total=qty * 100,
                      service_charge_rate=0, service_charge_amount=0,
                      customer_app_invite=1,
                      items=[Document(self, item="TEST-ITEM", qty=qty, rate=100, paid_qty=0)])
        values.update(overrides)
        doc = Document(self, **values)
        self.documents[doc.name] = doc
        return doc

    def call(self, endpoint, **data):
        self.frappe.local.form_dict = data
        with patch.dict(sys.modules, self.modules):
            return self.namespace[endpoint]()


class CustomerPaymentHooks(unittest.TestCase):
    def setUp(self):
        self.h = PaymentHarness()

    def place(self, **data):
        return self.h.call("place_order", items=[{"item": "TEST-ITEM", "qty": 1}], **data)

    def test_direct_sale_prepares_phone_and_automatic_invite_before_invoice(self):
        self.h.prepared = {"mobile": "0770000010", "customer_app_invite": 1}
        result = self.place(pos_customer=1, mobile="+94 77 000 0010")
        order = self.h.documents[result["name"]]
        self.assertEqual(result["mobile"], "0770000010")
        self.assertEqual(order.mobile, "0770000010")
        self.assertEqual(order.customer_app_invite, 1)
        self.assertEqual(self.h.events[0][0], "prepare_customer")
        self.h.prepare.assert_called_once()
        self.h.queue.assert_called_once_with(order.name)
        self.assertEqual(self.h.events[-1], ("queue", order.name))

    def test_registered_customer_metadata_disables_order_invite(self):
        self.h.prepared = {"mobile": "0770000010", "customer_app_invite": 0}
        result = self.place(pos_customer=1, mobile="0770000010")
        self.assertNotIn("customer_app_action", self.h.prepare.call_args.args[0])
        self.assertEqual(self.h.documents[result["name"]].customer_app_invite, 0)

    def test_legacy_direct_sale_keeps_original_mobile_without_new_fields(self):
        result = self.place(mobile="legacy-phone")
        order = self.h.documents[result["name"]]
        self.assertEqual(result["mobile"], "legacy-phone")
        self.assertEqual(order.mobile, "legacy-phone")
        self.assertNotIn("customer_app_invite", vars(order))

    def test_invalid_customer_metadata_never_creates_invoice_or_queues(self):
        self.h.prepare.side_effect = PaymentError("Invalid customer phone")
        with self.assertRaisesRegex(PaymentError, "Invalid customer phone"):
            self.place(pos_customer=1, mobile="invalid")
        self.assertEqual(self.h.inserted, [])
        self.h.queue.assert_not_called()

    def test_full_payment_locks_order_then_queues_after_completion(self):
        order = self.h.order(customer_app_invite=0)
        self.h.call("process_payment", order_name=order.name)
        self.assertEqual(self.h.reads[0], ("POS Order", order.name, {"for_update": True}))
        self.assertEqual(order.docstatus, 1)
        self.assertEqual(order.kitchen_status, "Served")
        self.h.queue.assert_called_once_with(order.name)
        self.h.prepare.assert_not_called()

    def test_partial_split_does_not_queue_and_final_split_does_once(self):
        order = self.h.order(qty=2, customer_app_invite=0)
        first = self.h.call("pay_split", order_name=order.name, lines=[{"item": "TEST-ITEM", "qty": 1}])
        self.assertFalse(first["fully_paid"])
        self.h.queue.assert_not_called()
        self.assertEqual(order.docstatus, 0)
        second = self.h.call("pay_split", order_name=order.name, lines=[{"item": "TEST-ITEM", "qty": 1}])
        self.assertTrue(second["fully_paid"])
        self.assertEqual(order.docstatus, 1)
        self.assertEqual(order.kitchen_status, "Served")
        self.h.queue.assert_called_once_with(order.name)

    def test_failed_direct_sale_submission_never_queues(self):
        self.h.fail_submit = True
        with self.assertRaisesRegex(PaymentError, "submission failed"):
            self.place()
        self.h.queue.assert_not_called()

    def test_failed_full_payment_submission_never_queues(self):
        order = self.h.order()
        self.h.fail_submit = True
        with self.assertRaisesRegex(PaymentError, "submission failed"):
            self.h.call("process_payment", order_name=order.name)
        self.assertEqual(order.pos_invoice, "")
        self.h.queue.assert_not_called()

    def test_failed_final_split_submission_never_queues_or_marks_paid(self):
        order = self.h.order(qty=1)
        self.h.fail_submit = True
        with self.assertRaisesRegex(PaymentError, "submission failed"):
            self.h.call("pay_split", order_name=order.name, lines=[{"item": "TEST-ITEM", "qty": 1}])
        self.assertEqual(order.items[0].paid_qty, 0)
        self.assertEqual(order.docstatus, 0)
        self.h.queue.assert_not_called()

    def test_failed_late_bookkeeping_does_not_queue(self):
        order = self.h.order()
        self.h.namespace["activity"].log_activity.side_effect = PaymentError("Bookkeeping failed")
        with self.assertRaisesRegex(PaymentError, "Bookkeeping failed"):
            self.h.call("process_payment", order_name=order.name)
        self.h.queue.assert_not_called()

    def test_permission_failure_never_prepares_customer_or_queues(self):
        self.h.require_pos.side_effect = PaymentError("Not permitted")
        with self.assertRaisesRegex(PaymentError, "Not permitted"):
            self.place(pos_customer=1, mobile="0770000010")
        self.h.prepare.assert_not_called()
        self.h.queue.assert_not_called()

    def test_wrong_branch_never_queues_or_creates_invoice(self):
        order = self.h.order()
        self.h.require_access.side_effect = PaymentError("Other restaurant")
        with self.assertRaisesRegex(PaymentError, "Other restaurant"):
            self.h.call("process_payment", order_name=order.name)
        self.assertEqual(self.h.inserted, [])
        self.h.queue.assert_not_called()

    def test_best_effort_queue_decline_does_not_fail_paid_response(self):
        order = self.h.order()
        self.h.queue.side_effect = None
        self.h.queue.return_value = False
        result = self.h.call("process_payment", order_name=order.name)
        self.assertEqual(result["status"], "Served")
        self.assertEqual(order.docstatus, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
