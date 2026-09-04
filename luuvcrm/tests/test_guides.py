"""Tour guide cashback: earning it, spending it, and the balance in between.

A guide brings customers and earns a percentage back; later they eat and pay
from that balance. So the balance is real money owed to a real person, and the
two directions have opposite failure modes.

Earning must never take a sale down with it — `_credit_guide_cashback` swallows
everything for that reason — but that also means a mistake here is silent, and
the only thing that notices is a guide whose balance is wrong.

Redeeming is the mirror image: `_parse_guide_redeem` throws on every problem,
deliberately, because a redemption the guide believes covered the bill must
never quietly do nothing.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class TestRedeemValidation(FrappeTestCase):
    """`_parse_guide_redeem` — the half that throws."""

    def test_no_request_is_not_a_redemption(self):
        self.assertEqual(api._parse_guide_redeem(None, 1000), (None, 0.0))
        self.assertEqual(api._parse_guide_redeem("", 1000), (None, 0.0))

    def test_a_request_for_nothing_is_not_a_redemption(self):
        guide = fx.guide(self, "GD-1", balance=500)
        self.assertEqual(api._parse_guide_redeem({"guide": guide, "amount": 0}, 1000),
                         (None, 0.0))

    def test_a_valid_request_is_accepted(self):
        guide = fx.guide(self, "GD-2", balance=500)
        self.assertEqual(api._parse_guide_redeem({"guide": guide, "amount": 300}, 1000),
                         (guide, 300.0))

    def test_it_reads_a_json_string_too(self):
        guide = fx.guide(self, "GD-3", balance=500)
        payload = json.dumps({"guide": guide, "amount": 200})
        self.assertEqual(api._parse_guide_redeem(payload, 1000), (guide, 200.0))

    def test_an_unknown_guide_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            api._parse_guide_redeem({"guide": "_LFT-NOBODY", "amount": 100}, 1000)
        self.assertIn("Guide not found", str(caught.exception))

    def test_spending_more_than_the_balance_is_refused(self):
        guide = fx.guide(self, "GD-4", balance=200)
        with self.assertRaises(frappe.ValidationError) as caught:
            api._parse_guide_redeem({"guide": guide, "amount": 500}, 1000)
        self.assertIn("balance is only", str(caught.exception))

    def test_spending_more_than_the_bill_is_refused(self):
        """Otherwise the till would be handing out change from a cashback
        balance."""
        guide = fx.guide(self, "GD-5", balance=5000)
        with self.assertRaises(frappe.ValidationError) as caught:
            api._parse_guide_redeem({"guide": guide, "amount": 2000}, 1000)
        self.assertIn("exceed the bill total", str(caught.exception))

    def test_the_whole_balance_may_be_spent(self):
        guide = fx.guide(self, "GD-6", balance=500)
        self.assertEqual(api._parse_guide_redeem({"guide": guide, "amount": 500}, 1000),
                         (guide, 500.0))

    def test_rounding_does_not_block_an_exact_spend(self):
        """The half-cent tolerance exists so a balance the client displays as
        499.99 is not refused against a stored 499.99000001."""
        guide = fx.guide(self, "GD-7", balance=499.99)
        self.assertEqual(api._parse_guide_redeem({"guide": guide, "amount": 499.99}, 1000),
                         (guide, 499.99))


class TestRedeeming(FrappeTestCase):
    """`_finalize_guide_redeem` — the half that moves the balance."""

    def test_it_takes_the_amount_off_the_balance(self):
        guide = fx.guide(self, "RD-1", balance=1000)
        api._finalize_guide_redeem(guide, 400, "ORD-1", "INV-1")
        self.assertEqual(fx.guide_balance(guide), 600)

    def test_it_records_what_was_spent_and_against_what(self):
        guide = fx.guide(self, "RD-2", balance=1000)
        api._finalize_guide_redeem(guide, 400, "ORD-2", "INV-2")
        txns = fx.guide_txns(guide, "Redeem")
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].amount, 400)
        self.assertEqual(txns[0].reference_invoice, "INV-2")

    def test_spending_nothing_does_nothing(self):
        guide = fx.guide(self, "RD-3", balance=1000)
        api._finalize_guide_redeem(guide, 0, "ORD-3", "INV-3")
        self.assertEqual(fx.guide_balance(guide), 1000)
        self.assertEqual(fx.guide_txns(guide), [])

    def test_no_guide_means_no_entry(self):
        api._finalize_guide_redeem(None, 500, "ORD-4", "INV-4")  # must not raise

    def test_two_redemptions_both_come_off(self):
        guide = fx.guide(self, "RD-5", balance=1000)
        api._finalize_guide_redeem(guide, 300, "ORD-5", "INV-5")
        api._finalize_guide_redeem(guide, 200, "ORD-6", "INV-6")
        self.assertEqual(fx.guide_balance(guide), 500)
        self.assertEqual(len(fx.guide_txns(guide, "Redeem")), 2)


class TestEarning(FrappeTestCase):
    """`_credit_guide_cashback` — the half that never raises."""

    def credit(self, guides, base, invoice, items=None, order="ORD"):
        api._credit_guide_cashback(guides, base, order, invoice, items=items)

    def test_a_guide_earns_their_percentage_of_the_bill(self):
        guide = fx.guide(self, "EA-1", percent=5)
        self.credit([guide], 2000, "INV-EA-1")
        self.assertEqual(fx.guide_balance(guide), 100)

    def test_the_earning_is_recorded_with_the_rate_used(self):
        guide = fx.guide(self, "EA-2", percent=10)
        self.credit([guide], 1500, "INV-EA-2")
        txn = fx.guide_txns(guide, "Earn")[0]
        self.assertEqual(txn.amount, 150)
        self.assertEqual(txn.bill_total, 1500)
        self.assertEqual(txn.percent_used, 10)

    def test_total_earned_climbs_with_the_balance(self):
        guide = fx.guide(self, "EA-3", percent=5)
        self.credit([guide], 1000, "INV-EA-3a")
        self.credit([guide], 1000, "INV-EA-3b")
        self.assertEqual(fx.guide_balance(guide), 100)
        self.assertEqual(float(frappe.db.get_value("Tour Guide", guide, "total_earned")), 100)

    def test_the_same_bill_credits_a_guide_once(self):
        """A client retry must not pay the commission twice."""
        guide = fx.guide(self, "EA-4", percent=5)
        self.credit([guide], 2000, "INV-EA-4")
        self.credit([guide], 2000, "INV-EA-4")
        self.assertEqual(fx.guide_balance(guide), 100)
        self.assertEqual(len(fx.guide_txns(guide, "Earn")), 1)

    def test_a_guide_earns_nothing_on_a_bill_they_paid_from_their_balance(self):
        """Otherwise eating on the house would top the balance back up."""
        guide = fx.guide(self, "EA-5", percent=5, balance=1000)
        api._finalize_guide_redeem(guide, 500, "ORD-EA-5", "INV-EA-5")
        self.credit([guide], 500, "INV-EA-5")
        self.assertEqual(fx.guide_txns(guide, "Earn"), [])
        self.assertEqual(fx.guide_balance(guide), 500)

    def test_several_guides_on_one_bill_each_earn(self):
        one = fx.guide(self, "EA-6a", percent=5)
        two = fx.guide(self, "EA-6b", percent=10)
        self.credit([one, two], 1000, "INV-EA-6")
        self.assertEqual(fx.guide_balance(one), 50)
        self.assertEqual(fx.guide_balance(two), 100)

    def test_the_same_guide_listed_twice_earns_once(self):
        guide = fx.guide(self, "EA-7", percent=5)
        self.credit([guide, guide], 1000, "INV-EA-7")
        self.assertEqual(fx.guide_balance(guide), 50)

    def test_only_the_first_few_guides_on_a_bill_are_paid(self):
        guides = [fx.guide(self, f"EA-8-{i}", percent=5)
                  for i in range(api.GUIDE_MAX_PER_BILL + 2)]
        self.credit(guides, 1000, "INV-EA-8")
        paid = [g for g in guides if fx.guide_balance(g) > 0]
        self.assertEqual(len(paid), api.GUIDE_MAX_PER_BILL)
        self.assertEqual(paid, guides[:api.GUIDE_MAX_PER_BILL],
                         "the cap must keep the order the cashier entered")

    def test_a_zero_percent_guide_earns_nothing(self):
        guide = fx.guide(self, "EA-9", percent=0)
        self.credit([guide], 5000, "INV-EA-9")
        self.assertEqual(fx.guide_balance(guide), 0)
        self.assertEqual(fx.guide_txns(guide), [])

    def test_a_bill_worth_nothing_earns_nothing(self):
        guide = fx.guide(self, "EA-10", percent=5)
        self.credit([guide], 0, "INV-EA-10")
        self.assertEqual(fx.guide_balance(guide), 0)

    def test_an_unknown_guide_is_skipped_not_fatal(self):
        real = fx.guide(self, "EA-11", percent=5)
        self.credit(["_LFT-NOBODY", real], 1000, "INV-EA-11")
        self.assertEqual(fx.guide_balance(real), 50,
                         "one bad name must not cost the others their commission")

    def test_a_broken_payload_never_takes_the_sale_down(self):
        """Everything here is wrapped: a cashback problem must not fail the
        payment it rides on."""
        api._credit_guide_cashback("{not json", 1000, "ORD", "INV-EA-12")
        api._credit_guide_cashback([{"unexpected": "shape"}], 1000, "ORD", "INV-EA-13")


class TestPerItemCommission(FrappeTestCase):
    """An item can carry its own commission rate, which beats the guide's."""

    def setUp(self):
        super().setUp()
        if not frappe.db.has_column("Item", "guide_commission_percent"):
            self.skipTest("Item.guide_commission_percent is not installed on this site")
        self.item = fx.item(self, "GD-ITEM")

    def test_an_item_rate_overrides_the_guide_rate(self):
        frappe.db.set_value("Item", self.item, "guide_commission_percent", 20)
        guide = fx.guide(self, "PI-1", percent=5)
        api._credit_guide_cashback([guide], 1000, "ORD", "INV-PI-1",
                                   items=[{"item": self.item, "qty": 1, "rate": 1000}])
        self.assertEqual(fx.guide_balance(guide), 200)

    def test_items_without_a_rate_fall_back_to_the_guide_s(self):
        frappe.db.set_value("Item", self.item, "guide_commission_percent", 0)
        guide = fx.guide(self, "PI-2", percent=5)
        api._credit_guide_cashback([guide], 1000, "ORD", "INV-PI-2",
                                   items=[{"item": self.item, "qty": 1, "rate": 1000}])
        self.assertEqual(fx.guide_balance(guide), 50)

    def test_a_discount_shrinks_the_commission_proportionally(self):
        """base_amount is subtotal minus discount, so the per-item maths is
        scaled by base/subtotal — a half-price bill pays half the commission."""
        frappe.db.set_value("Item", self.item, "guide_commission_percent", 10)
        guide = fx.guide(self, "PI-3", percent=5)
        api._credit_guide_cashback([guide], 500, "ORD", "INV-PI-3",
                                   items=[{"item": self.item, "qty": 1, "rate": 1000}])
        self.assertEqual(fx.guide_balance(guide), 50, "10% of 1000, halved by the discount")
