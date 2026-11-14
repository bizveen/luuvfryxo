"""Actual-source reward checkout/reversal tests with local transaction doubles.

No Frappe site, customer, order, SMS, or network is accessed. The doubles model
rollback and stale reads; real database isolation and lock scheduling need an
isolated Frappe integration test.
"""
import ast
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1] / "luuvcrm"
PHONE = "0771234567"
NOW = datetime(2026, 11, 14, 12)


class CheckoutError(Exception):
    pass


class Document(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)

    def append(self, field, values):
        getattr(self, field).append(Document(**values))

    def db_set(self, field, value):
        setattr(self, field, value)

    def save(self, **kwargs):
        return self

    def insert(self, **kwargs):
        self._site.insert(self)
        return self


def load_functions(relative, names, namespace):
    path = APP / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    for function in functions:
        function.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)


class CheckoutSite:
    def __init__(self, balance=1000, rate=1000, service_rate=10, discount=0):
        self.rows = {name: {} for name in ("POS Order", "Loyalty Customer", "Reward Redemption Request", "Cashback Transaction")}
        self.rate = rate
        self.service_rate = service_rate
        self.discount = discount
        self.reads = []
        self.commits = 0
        self.fail_insert = None
        self.stale_request = None
        self.before_wallet_read = None
        self.counter = 0
        self.wallet = self.document(doctype="Loyalty Customer", name="WALLET", mobile=PHONE,
                                    cashback_balance=balance, total_redeemed=0)
        self.rows["Loyalty Customer"]["WALLET"] = self.wallet
        self.db = SimpleNamespace(get_value=self.get_value, exists=self.exists,
            sql=Mock(return_value=[]), has_column=Mock(return_value=True),
            set_value=self.set_value, commit=self.commit)
        self.frappe = SimpleNamespace(local=SimpleNamespace(form_dict={}), db=self.db,
            get_doc=self.get_doc, parse_json=json.loads, throw=self.throw, log_error=Mock())
        self.token_mobile = Mock(side_effect=lambda token: PHONE if token == "valid-token" else None)
        self.record_offer = Mock(return_value="")
        self.ns = dict(frappe=self.frappe, _=lambda text: text,
            now_datetime=lambda: NOW, branch=SimpleNamespace(FIELD="restaurant", public=lambda **kw: "Kurunegala"),
            settings=SimpleNamespace(get=lambda key: None), PROMO_WINDOW_HOURS=24,
            _pending_promo_for=lambda phone: None, _apply_referral_for_mobile=lambda *a: {"success": True},
            _claim_first_order_referral_discount=lambda *a: {"applied": bool(self.discount), "amount": self.discount, "percent": 20},
            _clean_item_note=lambda value: value or "", _ensure_item_notes_field=lambda: None,
            _server_item_rate=lambda item: self.rate,
            _item_is_takeaway=lambda data, kind: kind == "Take Away" or bool(data.get("takeaway")),
            _is_takeaway=lambda kind: kind == "Take Away",
            _service_charge_on=lambda base: (self.service_rate if base else 0, round(base * self.service_rate / 100, 2)),
            prepare_customer_offer=lambda *a: {"applied": True, "offer": "OFFER", "order_discount": self.discount,
                                              "benefit_amount": self.discount, "title": "Test offer", "stackable": False},
            record_customer_offer=self.record_offer, _get_loyalty_settings=lambda: {"min_redeem": 200},
            _new_reward_code=self.reward_code, guest_order_push_ticket=lambda name: "push-ticket",
            send_order_whatsapp=Mock(), send_customer_whatsapp=Mock())
        load_functions("api/pricing.py", {"_apply_takeaway_service_charge"}, self.ns)
        load_functions("api/loyalty.py", {"_redeem_cashback", "reverse_reward_redemption", "finalize_reward_redemption"}, self.ns)
        load_functions("api/kiosk.py", {"kiosk_place_order"}, self.ns)
        self.modules = {}
        for name, exports in {
            "frappe.utils": {"get_datetime": datetime.fromisoformat,
                             "add_to_date": lambda dt, **kw: dt + timedelta(**kw)},
            "luuvcrm.api.customers": {"_mobile_from_token": self.token_mobile},
            "luuvcrm.api.inventory": {"consume_order_stock": Mock()},
            "luuvcrm.api.orders": {"track_signature": lambda name: "signature"},
        }.items():
            module = ModuleType(name)
            vars(module).update(exports)
            self.modules[name] = module

    def document(self, **values):
        return Document(_site=self, flags=SimpleNamespace(), docstatus=0, **values)

    def reward_code(self):
        self.counter += 1
        return f"{self.counter:06d}"

    def insert(self, document):
        if document.doctype == self.fail_insert:
            raise CheckoutError("Injected insert failure")
        self.counter += 1
        document.name = document.get("name") or document.get("code") or f"ROW-{self.counter}"
        self.rows[document.doctype][document.name] = document

    def get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            return self.document(**doctype)
        self.reads.append((doctype, name, kwargs))
        if doctype == "Loyalty Customer" and self.before_wallet_read:
            callback, self.before_wallet_read = self.before_wallet_read, None
            callback()
        return self.rows[doctype][name]

    def find(self, doctype, filters):
        if isinstance(filters, str):
            return self.rows[doctype].get(filters)
        return next((row for row in self.rows[doctype].values()
                     if all(row.get(key) == value for key, value in filters.items())), None)

    def get_value(self, doctype, filters, field):
        if doctype == "Item":
            return "Test Item"
        if doctype == "Reward Redemption Request" and self.stale_request:
            return self.stale_request
        row = self.find(doctype, filters)
        return row.get(field) if row else None

    def exists(self, doctype, filters):
        return bool(self.find(doctype, filters))

    def set_value(self, doctype, name, field, value=None):
        values = field if isinstance(field, dict) else {field: value}
        vars(self.rows[doctype][name]).update(values)

    def commit(self):
        self.commits += 1

    @staticmethod
    def throw(message):
        raise CheckoutError(message)

    def checkout(self, **values):
        data = dict(items=[{"item": "MEAL", "qty": 1}], customer_name="Test", mobile=PHONE,
                    customer_token="valid-token", order_channel="CUS", order_type="Take Away", use_rewards="1")
        data.update(values)
        self.frappe.local.form_dict = data
        # Snapshot values only; never deepcopy the site and its executable mocks.
        snapshot = {doctype: {name: {key: deepcopy(value) for key, value in vars(row).items()
                                     if key not in {"_site", "flags"}}
                              for name, row in rows.items()} for doctype, rows in self.rows.items()}
        try:
            with patch.dict(sys.modules, self.modules):
                return self.ns["kiosk_place_order"]()
        except Exception:
            if not self.commits:
                self.rows = {doctype: {name: Document(_site=self, flags=SimpleNamespace(), **fields)
                                       for name, fields in rows.items()} for doctype, rows in snapshot.items()}
                self.wallet = self.rows["Loyalty Customer"]["WALLET"]
            raise

    def reverse(self, name):
        return self.ns["reverse_reward_redemption"](name)

    def transactions(self, kind):
        return [row for row in self.rows["Cashback Transaction"].values() if row.type == kind]


class RewardCheckoutTests(unittest.TestCase):
    def test_token_owns_phone_and_server_owns_price_and_redemption_amount(self):
        site = CheckoutSite(balance=400)
        result = site.checkout(mobile="0770000001", reward_redeem_amount=99999,
                               items=[{"item": "MEAL", "qty": 1, "rate": 1}])
        self.assertEqual(result["mobile"], PHONE)
        self.assertEqual(result["reward_redeemed"], 400)
        self.assertEqual(result["grand_total"], 600)
        self.assertEqual(site.transactions("Redeem")[0].amount, 400)
        self.assertIn(("Loyalty Customer", "WALLET", {"for_update": True}), site.reads)

    def test_missing_or_expired_token_never_spends_or_commits(self):
        for token in ["", "expired-token"]:
            with self.subTest(token=token):
                site = CheckoutSite()
                if token:
                    self.assertIn("error", site.checkout(customer_token=token))
                else:
                    with self.assertRaisesRegex(CheckoutError, "Sign in"):
                        site.checkout(customer_token=token)
                self.assertEqual(site.wallet.cashback_balance, 1000)
                self.assertEqual(site.transactions("Redeem"), [])
                self.assertEqual(site.rows["POS Order"], {})
                self.assertEqual(site.commits, 0)

    def test_service_charge_is_not_debited_as_item_discount(self):
        site = CheckoutSite(balance=1100)
        result = site.checkout(order_type="Dine In")
        self.assertEqual(result["reward_redeemed"], 1000)
        self.assertEqual(result["grand_total"], 0)
        self.assertEqual(site.wallet.cashback_balance, 100)

    def test_referral_and_offer_discount_reduce_reward_cap_before_service_charge(self):
        for source in ["ref_code", "offer_name"]:
            with self.subTest(source=source):
                site = CheckoutSite(balance=1000, discount=200)
                result = site.checkout(order_type="Dine In", **{source: "TEST"})
                order = site.rows["POS Order"][result["name"]]
                self.assertEqual(result["reward_redeemed"], 800)
                self.assertEqual(order.discount_amount + order.reward_redeem_amount, 1000)
                self.assertEqual(result["grand_total"], 0)
                self.assertEqual(site.wallet.cashback_balance, 200)

    def test_small_app_balance_is_spendable_but_counter_minimum_remains(self):
        site = CheckoutSite(balance=50)
        result = site.checkout()
        self.assertEqual(result["reward_redeemed"], 50)
        self.assertEqual(result["grand_total"], 950)
        counter = CheckoutSite(balance=50)
        result = counter.ns["_redeem_cashback"](PHONE, 50)
        self.assertFalse(result["success"])
        self.assertEqual(counter.wallet.cashback_balance, 50)

    def test_declining_rewards_keeps_wallet_and_total(self):
        site = CheckoutSite()
        result = site.checkout(use_rewards="0")
        self.assertEqual(result["reward_redeemed"], 0)
        self.assertEqual(result["grand_total"], 1000)
        self.assertEqual(site.wallet.cashback_balance, 1000)

    def test_balance_changed_before_lock_is_rechecked_before_debit(self):
        site = CheckoutSite()
        site.before_wallet_read = lambda: setattr(site.wallet, "cashback_balance", 100)
        with self.assertRaisesRegex(CheckoutError, "Insufficient balance"):
            site.checkout()
        self.assertEqual(site.transactions("Redeem"), [])
        self.assertEqual(site.commits, 0)

    def test_failure_after_wallet_debit_has_no_commit_and_rolls_back_the_unit(self):
        for failure in ["request", "offer_record"]:
            with self.subTest(failure=failure):
                site = CheckoutSite()
                if failure == "request":
                    site.fail_insert = "Reward Redemption Request"
                else:
                    site.record_offer.side_effect = CheckoutError("Offer persistence failed")
                with self.assertRaises(CheckoutError):
                    site.checkout()
                self.assertEqual(site.wallet.cashback_balance, 1000)
                self.assertEqual(site.rows["POS Order"], {})
                self.assertEqual(site.rows["Reward Redemption Request"], {})
                self.assertEqual(site.transactions("Redeem"), [])
                self.assertEqual(site.commits, 0)


class RewardReversalTests(unittest.TestCase):
    def setUp(self):
        self.site = CheckoutSite(balance=400)
        result = self.site.checkout()
        self.order = self.site.rows["POS Order"][result["name"]]
        self.request = self.site.rows["Reward Redemption Request"][self.order.reward_redemption_request]
        self.site.reads.clear()

    def test_cancellation_restores_once_with_order_request_and_wallet_locks(self):
        self.assertTrue(self.site.reverse(self.order.name))
        self.assertEqual(self.site.wallet.cashback_balance, 400)
        self.assertEqual(self.request.status, "Cancelled")
        self.assertFalse(self.site.reverse(self.order.name))
        self.assertEqual(len(self.site.transactions("Reversal")), 1)
        self.assertEqual(self.site.reads[:3], [
            ("POS Order", self.order.name, {"for_update": True}),
            ("Reward Redemption Request", self.request.name, {"for_update": True}),
            ("Loyalty Customer", "WALLET", {"for_update": True})])

    def test_stale_approved_lookup_cannot_restore_a_cancelled_or_redeemed_request(self):
        for status in ["Cancelled", "Redeemed"]:
            with self.subTest(status=status):
                self.request.status = status
                self.site.stale_request = self.request.name
                self.assertFalse(self.site.reverse(self.order.name))
                self.assertEqual(self.site.wallet.cashback_balance, 0)
                self.assertEqual(self.site.transactions("Reversal"), [])

    def test_paid_order_cannot_restore_rewards_even_before_request_finalization(self):
        self.order.pos_invoice = "PAID-INVOICE"
        self.assertFalse(self.site.reverse(self.order.name))
        self.assertEqual(self.site.wallet.cashback_balance, 0)
        self.assertEqual(self.request.status, "Approved")
        self.assertEqual(self.site.transactions("Reversal"), [])

    def test_payment_finalizes_existing_debit_without_spending_again(self):
        self.order.pos_invoice = "PAID-INVOICE"
        self.site.ns["finalize_reward_redemption"](self.order.name, "PAID-INVOICE")
        self.assertEqual(self.request.status, "Redeemed")
        self.assertEqual(self.request.pos_invoice, "PAID-INVOICE")
        self.assertEqual(len(self.site.transactions("Redeem")), 1)
        self.assertFalse(self.site.reverse(self.order.name))

    def test_missing_wallet_still_cancels_the_audit_request(self):
        self.site.rows["Loyalty Customer"].clear()
        self.assertFalse(self.site.reverse(self.order.name))
        self.assertEqual(self.request.status, "Cancelled")
        self.assertEqual(self.site.transactions("Reversal"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
