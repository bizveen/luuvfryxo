"""What an item costs, and what the service charge is on top of it.

This is the money path. Every order endpoint prices its lines through
`_server_item_rate`, and until Phase 1 they took the rate from the request
instead — so these tests are both a regression net for that fix and the
specification of a resolver that had none.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class TestRateResolution(FrappeTestCase):
    """`_resolve_item_rate` priority: a valid Item Price in the asked-for price
    list, then any valid selling Item Price, then the menu row, then
    Item.standard_rate."""

    def test_item_price_in_the_named_list_wins(self):
        code = fx.item(self, "RATE-1", standard_rate=100)
        fx.item_price(self, code, 250, price_list="Standard Selling")
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 250)

    def test_a_price_in_another_list_is_used_when_the_named_one_has_none(self):
        code = fx.item(self, "RATE-2", standard_rate=100)
        other = fx.price_list(self, "Other")
        fx.item_price(self, code, 310, price_list=other)
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 310)

    def test_the_named_list_beats_another_list(self):
        code = fx.item(self, "RATE-3")
        other = fx.price_list(self, "Other")
        fx.item_price(self, code, 999, price_list=other)
        fx.item_price(self, code, 400, price_list="Standard Selling")
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 400)

    def test_the_menu_row_is_used_when_there_is_no_item_price(self):
        code = fx.item(self, "RATE-4", standard_rate=100)
        self.assertEqual(api._resolve_item_rate(code, 175, "Standard Selling"), 175)

    def test_standard_rate_is_the_last_resort(self):
        code = fx.item(self, "RATE-5", standard_rate=90)
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 90)

    def test_an_item_priced_nowhere_resolves_to_zero(self):
        code = fx.item(self, "RATE-6", standard_rate=0)
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 0)


class TestPriceValidity(FrappeTestCase):
    """Item Price carries a validity window and an optional customer. The
    resolver honours both — a stale price must not reach a till."""

    def test_an_expired_price_is_ignored(self):
        code = fx.item(self, "VALID-1", standard_rate=60)
        # ERPNext defaults a blank valid_from to today, so an expired price has
        # to carry both ends or its own validation rejects the range.
        fx.item_price(self, code, 500, valid_from=fx.days_ago(30), valid_upto=fx.yesterday())
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 60,
                         "a price that expired yesterday must not be charged today")

    def test_a_price_that_starts_tomorrow_is_ignored(self):
        code = fx.item(self, "VALID-2", standard_rate=60)
        fx.item_price(self, code, 500, valid_from=fx.tomorrow())
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 60)

    def test_a_price_valid_today_is_used(self):
        code = fx.item(self, "VALID-3", standard_rate=60)
        fx.item_price(self, code, 500, valid_from=fx.yesterday(), valid_upto=fx.tomorrow())
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 500)

    def test_a_customer_specific_price_is_skipped(self):
        code = fx.item(self, "VALID-4", standard_rate=60)
        customer = frappe.db.get_value("Customer", {}, "name")
        if not customer:
            self.skipTest("no Customer on this site")
        fx.item_price(self, code, 500, customer=customer)
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 60,
                         "a deal for one customer must not become the menu price")

    def test_a_buying_price_is_never_charged_to_a_diner(self):
        code = fx.item(self, "VALID-5", standard_rate=60)
        fx.item_price(self, code, 12, price_list="Standard Buying", selling=0)
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 60,
                         "the cost price must never be billed as the sale price")

    def test_the_newest_validity_window_wins(self):
        code = fx.item(self, "VALID-6")
        fx.item_price(self, code, 100, valid_from=fx.yesterday())
        fx.item_price(self, code, 200, valid_from=frappe.utils.today())
        self.assertEqual(api._resolve_item_rate(code, 0, "Standard Selling"), 200)


class TestServerItemRate(FrappeTestCase):
    """`_server_item_rate` is what every order path calls. It must reach the same
    answer as the page does, with no help from the client."""

    def test_it_falls_back_to_the_menu_row(self):
        """The reason a client rate existed: an item can be priced only here."""
        code = fx.item(self, "SRV-1", standard_rate=0)
        fx.menu_rate(self, code, 640)
        self.assertEqual(api._server_item_rate(code), 640,
                         "an item priced only on the menu must not resolve to 0")

    def test_an_item_price_beats_the_menu_row(self):
        code = fx.item(self, "SRV-2", standard_rate=0)
        fx.menu_rate(self, code, 640)
        fx.item_price(self, code, 700)
        self.assertEqual(api._server_item_rate(code), 700,
                         "Item Price is the authority — a price change must reach the POS")

    def test_it_matches_what_the_page_renders(self):
        """pos.py renders `_resolve_item_rate(code, menu_row.rate, price_list)`.
        If the two ever disagree, the customer is quoted one price and billed
        another."""
        code = fx.item(self, "SRV-3", standard_rate=0)
        fx.menu_rate(self, code, 425)
        page = api._resolve_item_rate(code, 425, api._get_pos_price_list())
        self.assertEqual(api._server_item_rate(code), page)

    def test_the_menu_is_read_once_per_request(self):
        frappe.local._lf_menu_rates = None
        first = api._menu_row_rates()
        self.assertIs(api._menu_row_rates(), first, "the menu lookup must be cached")

    def test_an_unknown_item_resolves_to_zero_rather_than_raising(self):
        self.assertEqual(api._server_item_rate("_LFT-DOES-NOT-EXIST"), 0)


class TestTakeawayDetection(FrappeTestCase):
    """Take-away carries no service charge, so what counts as take-away is a
    money question."""

    def test_order_type_take_away_in_any_spelling(self):
        for value in ("Take Away", "take away", "Takeaway", "  TAKEAWAY  "):
            with self.subTest(order_type=value):
                self.assertTrue(api._is_takeaway(value))

    def test_dine_in_and_delivery_are_not_take_away(self):
        for value in ("Dine In", "Delivery", "", None):
            with self.subTest(order_type=value):
                self.assertFalse(api._is_takeaway(value))

    def test_a_line_can_be_parcelled_out_of_a_dine_in_order(self):
        self.assertTrue(api._item_is_takeaway({"takeaway": 1}, "Dine In"))
        self.assertFalse(api._item_is_takeaway({"takeaway": 0}, "Dine In"))

    def test_every_line_of_a_take_away_order_is_take_away(self):
        self.assertTrue(api._item_is_takeaway({"takeaway": 0}, "Take Away"))


class TestServiceCharge(FrappeTestCase):
    def test_it_is_a_percentage_of_the_dine_in_subtotal(self):
        fx.service_charge(self, 10)
        self.assertEqual(api._service_charge_on(1000), (10.0, 100.0))

    def test_nothing_is_charged_on_nothing(self):
        fx.service_charge(self, 10)
        self.assertEqual(api._service_charge_on(0), (0.0, 0.0))

    def test_a_zero_rate_charges_nothing(self):
        fx.service_charge(self, 0)
        rate, amount = api._service_charge_on(1000)
        self.assertEqual((rate, amount), (0.0, 0.0))

    def test_it_is_rounded_to_cents(self):
        fx.service_charge(self, 7.5)
        _, amount = api._service_charge_on(333.33)
        self.assertEqual(amount, 25.0)

    def test_a_take_away_order_is_never_charged(self):
        fx.service_charge(self, 10)
        self.assertEqual(api._sc_rate_for("Take Away"), 0.0)
        self.assertEqual(api._sc_amount_for(1000, "Take Away"), 0.0)

    def test_a_dine_in_order_is(self):
        fx.service_charge(self, 10)
        self.assertEqual(api._sc_rate_for("Dine In"), 10.0)
        self.assertEqual(api._sc_amount_for(1000, "Dine In"), 100.0)
