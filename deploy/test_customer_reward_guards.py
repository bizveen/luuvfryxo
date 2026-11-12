"""Offline regressions for the merge's reward/payment safety boundaries.

Runs the actual endpoint ASTs with in-memory Frappe records. No network, site,
database, invoice, or wallet outside this process is touched. --ref can exercise
an unmodified Git version to establish that these cases catch the regressions.
"""

import argparse
import ast
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch


PARSER = argparse.ArgumentParser()
PARSER.add_argument("--ref")
ARGS, TEST_ARGS = PARSER.parse_known_args()
REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 12, 12, 0)


def endpoint(relative, name, context):
    if ARGS.ref:
        source = subprocess.check_output([
            "git", "-c", "safe.directory=" + REPO.as_posix(), "-C", str(REPO),
            "show", ARGS.ref + ":" + relative,
        ]).decode("utf-8")
    else:
        source = (REPO / relative).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), relative, "exec"), context)
    return context[name]


class Record(dict):
    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value

    def db_set(self, key, value):
        self[key] = value


class ValidationError(Exception):
    pass


class InvoiceFlowReached(Exception):
    """The unchanged invoice path begins after the boundary being tested."""


class MemorySite:
    def __init__(self):
        self.balance = 1000.0
        self.debits = []
        self.access = []
        self.order_reads = []
        self.list_queries = []
        self.order = Record(name="ORDER-1", restaurant="Kurunegala", mobile="0770000001",
                            docstatus=0, pos_invoice="", grand_total=1000,
                            items=[Record(item="MEAL", qty=1, rate=1000, paid_qty=0)])
        self.other_order = Record(name="ORDER-2", restaurant="Luuv Fryxo", mobile="0770000002",
                                  docstatus=0, pos_invoice="", grand_total=1000)
        self.requests = {code: Record(name=code, code=code, status="Pending",
                                     expires_on=NOW + timedelta(minutes=10),
                                     mobile=self.order.mobile, amount=200)
                         for code in ("111111", "222222")}
        self.form = Record(code="111111", order_name=self.order.name,
                           lines=json.dumps([{"item": "MEAL", "qty": 1}]))
        self.frappe = types.SimpleNamespace(
            local=types.SimpleNamespace(form_dict=self.form),
            session=types.SimpleNamespace(user="branch.manager@example.test"),
            db=types.SimpleNamespace(exists=self.exists, sql=self.sql),
            get_doc=self.get_doc, get_value=lambda *args: "Branch manager",
            get_all=self.get_all, get_list=self.get_list,
            parse_json=json.loads, throw=self.throw,
        )
        self.branch = types.SimpleNamespace(
            FIELD="restaurant", viewing=lambda: "Kurunegala",
            require_access=self.require_access,
        )

    def exists(self, doctype, name):
        if doctype == "Reward Redemption Request":
            return name in self.requests
        if doctype == "POS Order":
            return name == self.order.name
        return False

    def sql(self, query, *args, **kwargs):
        if "COUNT(*) AS customers" in query:
            return [Record(customers=1, outstanding=self.balance, earned=1000, redeemed=0)]
        return []

    def get_doc(self, doctype, name, **kwargs):
        if doctype == "Reward Redemption Request":
            return self.requests[name]
        if doctype == "POS Order":
            self.order_reads.append(kwargs.get("for_update", False))
            return self.order
        raise AssertionError("Unexpected document read: " + doctype)

    def get_all(self, doctype, **kwargs):
        if doctype == "POS Order":
            return [self.order, self.other_order]
        return []

    def get_list(self, doctype, **kwargs):
        self.list_queries.append((doctype, kwargs))
        return [row for row in (self.order, self.other_order)
                if row.restaurant == kwargs.get("filters", {}).get("restaurant")]

    def require_access(self, doctype, name):
        self.access.append((doctype, name))
        if self.order.restaurant != "Kurunegala":
            raise PermissionError("Other branch")

    def redeem(self, mobile, amount, *args, **kwargs):
        self.balance -= amount
        self.debits.append(amount)
        return {"success": True, "new_balance": self.balance}

    @staticmethod
    def throw(message):
        raise ValidationError(message)

    def context(self):
        return dict(
            frappe=self.frappe, branch=self.branch,
            roles=types.SimpleNamespace(require_manager=lambda: None, require_pos=lambda: None),
            now_datetime=lambda: NOW, _local_number=lambda number: number,
            _redeem_cashback=self.redeem, _expire_reward_requests=lambda: None,
            _get_loyalty_settings=lambda: {}, _require_cus_call_released=lambda order: None,
            _active_pos_profile_name=self.invoice_flow, _=lambda value: value,
        )

    @staticmethod
    def invoice_flow(*args):
        raise InvoiceFlowReached()


class RewardMergeTests(unittest.TestCase):
    def setUp(self):
        self.site = MemorySite()
        pricing = types.ModuleType("luuvcrm.api.pricing")
        pricing._apply_takeaway_service_charge = lambda order: order.db_set(
            "grand_total", 1000 - float(order.get("reward_redeem_amount") or 0))
        self.modules = patch.dict(sys.modules, {"luuvcrm.api.pricing": pricing})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.redeem = endpoint("luuvcrm/api/loyalty.py", "manager_redeem_reward", self.site.context())
        self.split = endpoint("luuvcrm/api/payments.py", "pay_split", self.site.context())

    def test_valid_reward_debits_once_and_keeps_the_discount(self):
        result = self.redeem()
        self.assertTrue(result["success"])
        self.assertEqual(self.site.balance, 800)
        self.assertEqual(self.site.order.grand_total, 800)
        self.assertEqual(self.site.order.reward_redemption_request, "111111")
        self.assertEqual(self.site.requests["111111"].status, "Approved")
        self.assertEqual(self.site.access, [("POS Order", "ORDER-1")])
        self.assertEqual(self.site.order_reads, [True])

    def test_second_code_cannot_debit_the_same_order_again(self):
        self.assertTrue(self.redeem()["success"])
        self.site.form.code = "222222"
        result = self.redeem()
        self.assertFalse(result["success"])
        self.assertEqual(self.site.debits, [200])
        self.assertEqual(self.site.order.reward_redemption_request, "111111")
        self.assertEqual(self.site.requests["222222"].status, "Pending")

    def test_reward_cannot_be_applied_after_any_split_payment(self):
        self.site.order["items"][0].paid_qty = 1
        self.assertFalse(self.redeem()["success"])
        self.assertEqual(self.site.balance, 1000)
        self.assertEqual(self.site.requests["111111"].status, "Pending")

    def test_other_branch_order_is_refused_before_wallet_or_order_write(self):
        self.site.order.restaurant = "Luuv Fryxo"
        with self.assertRaises(PermissionError):
            self.redeem()
        self.assertEqual(self.site.balance, 1000)
        self.assertEqual(self.site.order_reads, [])
        self.assertEqual(self.site.requests["111111"].status, "Pending")

    def test_reward_dashboard_only_lists_current_branch_orders(self):
        dashboard = endpoint("luuvcrm/api/loyalty.py", "manager_get_rewards", self.site.context())
        result = dashboard()
        self.assertEqual([row.name for row in result["open_orders"]], ["ORDER-1"])
        self.assertEqual(len(self.site.list_queries), 1)

    def test_split_refuses_each_reward_or_offer_marker_before_invoice(self):
        cases = {"reward_redeem_amount": 200, "reward_redemption_request": "111111",
                 "customer_offer": "LUNCH", "offer_discount_amount": 50}
        for field, value in cases.items():
            with self.subTest(field=field):
                self.site.order[field] = value
                try:
                    with self.assertRaisesRegex(ValidationError, "paid in full"):
                        self.split()
                    self.assertEqual(self.site.balance, 1000)
                finally:
                    del self.site.order[field]

    def test_plain_split_still_reaches_invoice_path_with_order_lock(self):
        with self.assertRaises(InvoiceFlowReached):
            self.split()
        self.assertEqual(self.site.order_reads, [True])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *TEST_ARGS], verbosity=2)
