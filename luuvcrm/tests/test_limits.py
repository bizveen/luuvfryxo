"""P3: what is capped, what must never be.

`frappe.get_all` with no `limit_page_length` reads the whole table. Most of the
app's reads are bounded by a filter — one table, one shift, one day — but the
ones filtered on a *status* are not: an order stuck in Draft, an approval nobody
answers, a customer's whole order history. Those are read again on every poll,
for the life of the site, so they carry an explicit ceiling now.

The other half of this file is the more important half. Adding a limit to a read
that is being **summed** does not make it faster, it makes the number wrong — so
these tests put more rows through the money reads than any ceiling in the app and
assert the totals still come out whole.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.api import orders as orders_mod
from luuvcrm.api import customers as customers_mod
from luuvcrm.tests import fixtures as fx


class LimitTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.item = fx.item(self, "LIM-BURGER")
        fx.item_price(self, self.item, 100)
        fx.service_charge(self, 0)
        self._orders = []
        self.addCleanup(self._drop)

    def _drop(self):
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

    def sell(self, qty=1, **kwargs):
        result = self.call(api.send_to_kitchen,
                           items=json.dumps([{"item": self.item, "qty": qty}]),
                           order_type="Dine In")
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        paid = self.call(api.process_payment, order_name=name,
                         payment_mode="Cash", **kwargs)
        fx.drop_invoice(self, paid["invoice_name"])
        return paid


class TestTheMoneyReadsAreNotCapped(LimitTestCase):
    """The failure this guards against is silent: a ceiling on a summed read
    makes the day's revenue wrong, and nothing on the screen says so."""

    #: More sales than the smallest ceiling in the app, so a cap would show.
    SALES = 12

    def test_the_day_s_revenue_counts_every_sale(self):
        before = api.get_manager_dashboard()
        for _ in range(self.SALES):
            self.sell()
        after = api.get_manager_dashboard()
        self.assertEqual(after["sales_count"] - before["sales_count"], self.SALES)
        self.assertEqual(after["revenue"] - before["revenue"], 100 * self.SALES)

    def test_the_payment_mix_counts_every_sale(self):
        cash = lambda d: next((p["amount"] for p in d["payments"] if p["mode"] == "Cash"), 0)
        before = cash(api.get_manager_dashboard())
        for _ in range(self.SALES):
            self.sell()
        self.assertEqual(cash(api.get_manager_dashboard()) - before, 100 * self.SALES)

    def test_the_item_tally_counts_every_sale(self):
        for _ in range(self.SALES):
            self.sell(qty=2)
        row = next((t for t in api.get_manager_dashboard()["top_items"]
                    if t["name"] == self.item), None)
        if row is None:
            self.skipTest("the day's top five is fuller than this test's sale")
        self.assertEqual(row["qty"], 2 * self.SALES)

    def test_the_day_report_agrees_with_the_dashboard(self):
        for _ in range(self.SALES):
            self.sell()
        day = api.get_manager_day_report(date=frappe.utils.today())
        dash = api.get_manager_dashboard()
        self.assertEqual(day["revenue"], dash["revenue"])
        self.assertEqual(day["sales_count"], dash["sales_count"])


class TestTheStatusReadsAreCapped(FrappeTestCase):
    """These filter on a status, not a date, so nothing retires a row from them."""

    def test_the_polled_screens_declare_a_ceiling(self):
        self.assertGreater(orders_mod.SCREEN_ORDER_CEILING, 0)
        self.assertGreater(orders_mod.OPEN_ORDER_CEILING, orders_mod.SCREEN_ORDER_CEILING)

    def test_every_polled_order_screen_asks_for_a_bounded_page(self):
        """A ceiling that one screen forgets is the screen that falls over."""
        import inspect
        for fn in (api.get_ongoing_orders, api.get_kiosk_orders,
                   api.get_kitchen_orders, api.get_tables_with_status):
            with self.subTest(endpoint=fn.__name__):
                self.assertIn("limit_page_length", inspect.getsource(fn))

    def test_a_customer_gets_a_page_of_history_not_the_table(self):
        self.assertEqual(customers_mod.CUSTOMER_ORDER_PAGE, 20)
        self.assertLessEqual(customers_mod.CUSTOMER_ORDER_PAGE,
                             customers_mod.CUSTOMER_ORDER_MAX)

    def test_the_ongoing_count_is_asked_as_a_count(self):
        """The POS page plucked every draft order's name to call len() on it —
        and drafts are never cleared."""
        source = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "pos.py"))
        self.assertIn('frappe.db.count("POS Order"', source)
        self.assertNotIn('pluck="name"', source)
