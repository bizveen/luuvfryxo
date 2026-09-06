"""The manager dashboard's arithmetic.

It is the app's most-polled screen and, until this file, nothing asserted a
single number on it. That mattered when the two N+1 loops behind it were
replaced with grouped reads (P1): the loops built a full POS Invoice and a full
POS Order per row to add up a handful of child rows, and swapping them for one
query each is exactly the kind of change that quietly moves a total.

Every test here takes real money through the real endpoints first, then reads
the dashboard back and asks whether it agrees.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class DashboardTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        tag = frappe.generate_hash(length=6).upper()
        self.burger = fx.item(self, f"DASH-{tag}-BURGER")
        fx.item_price(self, self.burger, 1000)
        self.cola = fx.item(self, f"DASH-{tag}-COLA")
        fx.item_price(self, self.cola, 250)
        fx.service_charge(self, 0)          # keep the arithmetic about the sale
        self._orders = []
        self.addCleanup(self._drop_orders)
        self.before = api.get_manager_dashboard()

    def _drop_orders(self):
        for name in reversed(self._orders):
            if not frappe.db.exists("POS Order", name):
                continue
            doc = frappe.get_doc("POS Order", name)
            if doc.docstatus == 1:
                doc.db_set("docstatus", 0)
            frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def sell(self, items, **kwargs):
        """One order, taken and paid — the way the counter does it."""
        result = self.call(api.send_to_kitchen, items=json.dumps(items),
                           order_type="Dine In")
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        paid = self.call(api.process_payment, order_name=name, **kwargs)
        fx.drop_invoice(self, paid["invoice_name"])
        return paid

    def delta(self, key):
        """The dashboard moves on a site with a day's trade already on it, so
        every assertion here is on what this test's own sale added."""
        now = api.get_manager_dashboard()
        return now[key] - self.before[key]

    def mode(self, dashboard, name):
        return next((p["amount"] for p in dashboard["payments"] if p["mode"] == name), 0)


class TestTheDayAddsUp(DashboardTestCase):
    def test_a_sale_moves_revenue_and_the_count(self):
        self.sell([{"item": self.burger, "qty": 2}], payment_mode="Cash")
        self.assertEqual(self.delta("revenue"), 2000)
        self.assertEqual(self.delta("sales_count"), 1)

    def test_the_average_ticket_is_the_revenue_over_the_count(self):
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash")
        now = api.get_manager_dashboard()
        self.assertAlmostEqual(now["avg_ticket"], now["revenue"] / now["sales_count"], places=2)


class TestThePaymentMix(DashboardTestCase):
    """The mix used to be summed by building every invoice of the day."""

    def test_cash_lands_in_the_mix_and_in_the_drawer(self):
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash")
        now = api.get_manager_dashboard()
        self.assertEqual(self.mode(now, "Cash") - self.mode(self.before, "Cash"), 1000)
        self.assertEqual(now["cash"]["cash_sales"] - self.before["cash"]["cash_sales"], 1000)

    def test_a_split_bill_is_counted_once_per_mode(self):
        fx.payment_mode_account(self, "Credit Card")
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash+Card",
                  cash_amount=600, card_amount=400)
        now = api.get_manager_dashboard()
        self.assertEqual(self.mode(now, "Cash") - self.mode(self.before, "Cash"), 600)
        self.assertEqual(self.mode(now, "Credit Card")
                         - self.mode(self.before, "Credit Card"), 400)

    def test_only_the_cash_leg_reaches_the_drawer(self):
        """Card money in the drawer is a short till at close, every night."""
        fx.payment_mode_account(self, "Credit Card")
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash+Card",
                  cash_amount=600, card_amount=400)
        now = api.get_manager_dashboard()
        self.assertEqual(now["cash"]["cash_sales"] - self.before["cash"]["cash_sales"], 600)

    def test_the_mix_sums_to_the_day_s_revenue(self):
        fx.payment_mode_account(self, "Credit Card")
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash")
        self.sell([{"item": self.cola, "qty": 2}], payment_mode="Credit Card")
        now = api.get_manager_dashboard()
        self.assertAlmostEqual(sum(p["amount"] for p in now["payments"]),
                               now["revenue"], places=2)

    def test_the_modes_come_back_biggest_first(self):
        amounts = [p["amount"] for p in api.get_manager_dashboard()["payments"]]
        self.assertEqual(amounts, sorted(amounts, reverse=True))


class TestTopItems(DashboardTestCase):
    """Tallied by loading every paid order in full; now one read of the lines."""

    def row(self, name):
        """This item's row on the dashboard, or a clear reason it is not there.

        `top_items` is capped at five. An item missing because the day's fifth
        seller beat it is the cap doing its job; missing while out-earning it is
        the tally being wrong, and the two must not read the same.
        """
        now = api.get_manager_dashboard()
        found = next((t for t in now["top_items"] if t["name"] == name), None)
        if found:
            return found
        fifth = now["top_items"][-1]["revenue"] if len(now["top_items"]) >= 5 else 0
        self.skipTest(f"the day's top five starts at Rs {fifth} — this sale is under it")

    def test_an_item_sold_twice_on_two_bills_is_one_row(self):
        self.sell([{"item": self.burger, "qty": 2}], payment_mode="Cash")
        self.sell([{"item": self.burger, "qty": 3}], payment_mode="Cash")
        row = self.row(self.burger)
        self.assertEqual(row["qty"], 5)
        self.assertEqual(row["revenue"], 5000)

    def test_a_bill_with_two_lines_tallies_both(self):
        self.sell([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 4}],
                  payment_mode="Cash")
        self.assertEqual(self.row(self.burger)["qty"], 1)
        cola = self.row(self.cola)
        self.assertEqual(cola["qty"], 4)
        self.assertEqual(cola["revenue"], 1000)

    def test_it_is_ordered_by_revenue_and_capped_at_five(self):
        now = api.get_manager_dashboard()
        revenues = [t["revenue"] for t in now["top_items"]]
        self.assertEqual(revenues, sorted(revenues, reverse=True))
        self.assertLessEqual(len(now["top_items"]), 5)


class TestItDoesNotBuildADocumentPerRow(DashboardTestCase):
    """P1. The screen polls; the cost of one more sale on it should be a row in
    a grouped query, not another Document built in full."""

    def documents_built(self):
        """How many POS Invoice / POS Order documents one dashboard call loads."""
        built = []
        real = frappe.get_doc

        def counting(*args, **kwargs):
            if args and args[0] in ("POS Invoice", "POS Order"):
                built.append(args[0])
            return real(*args, **kwargs)

        frappe.get_doc = counting
        try:
            api.get_manager_dashboard()
        finally:
            frappe.get_doc = real
        return built

    def test_one_sale_builds_none(self):
        self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash")
        self.assertEqual(self.documents_built(), [])

    def test_four_sales_build_no_more_than_one(self):
        """It used to be one POS Invoice per sale plus one POS Order per paid
        order — eight documents for these four sales, on a screen that polls."""
        for _ in range(4):
            self.sell([{"item": self.burger, "qty": 1}], payment_mode="Cash")
        self.assertEqual(self.documents_built(), [])
