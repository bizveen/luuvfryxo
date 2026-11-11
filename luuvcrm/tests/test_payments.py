"""Taking money: paying a bill in full, and paying it in parts.

The highest-risk code in the app and, until now, the least covered. Every test
here runs the real endpoint and then reads the invoice and the order back,
because the failure that matters is not an exception — it is a bill that
balances on screen and is wrong in the ledger.

Split payment is the subtle half. `paid_qty` on each order line is the only
record of what has already been billed, so an error there either bills a
customer twice or lets a table walk out with food nobody charged for.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class PaymentTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.burger = fx.item(self, "PAY-BURGER")
        fx.item_price(self, self.burger, 1000)
        self.cola = fx.item(self, "PAY-COLA")
        fx.item_price(self, self.cola, 250)
        fx.service_charge(self, 10)
        self._orders = []
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        for name in reversed(self._orders):
            if not frappe.db.exists("POS Order", name):
                continue
            try:
                doc = frappe.get_doc("POS Order", name)
                if doc.docstatus == 1:
                    doc.db_set("docstatus", 0)
                frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
            except Exception:
                frappe.db.rollback()

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def order(self, items, order_type="Dine In", **kwargs):
        result = self.call(api.send_to_kitchen, items=json.dumps(items),
                           order_type=order_type, **kwargs)
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        return frappe.get_doc("POS Order", name)

    def pay(self, order, **kwargs):
        result = self.call(api.process_payment, order_name=order.name, **kwargs)
        fx.drop_invoice(self, result["invoice_name"])
        return result

    def split(self, order, lines, **kwargs):
        result = self.call(api.pay_split, order_name=order.name,
                           lines=json.dumps(lines), **kwargs)
        if result.get("invoice_name"):
            fx.drop_invoice(self, result["invoice_name"])
        return result


class TestPayInFull(PaymentTestCase):
    def test_it_raises_an_invoice_for_the_order_total(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        result = self.pay(order)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual(invoice.grand_total, 2200, "2000 of food plus 10% service")

    def test_the_invoice_is_linked_back_to_the_order(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order)
        order.reload()
        self.assertEqual(order.pos_invoice, result["invoice_name"],
                         "without the link the order looks unpaid forever")

    def test_full_payment_completes_the_order_for_the_customer_app(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order)
        order.reload()
        self.assertEqual(result["status"], "Served")
        self.assertEqual(order.kitchen_status, "Served")
        self.assertEqual(order.docstatus, 1)

    def test_the_invoice_is_submitted_so_it_reaches_the_shift(self):
        """A draft invoice never consolidates at shift close, so the sale would
        be invisible in the day's takings."""
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order)
        self.assertEqual(frappe.db.get_value("POS Invoice", result["invoice_name"], "docstatus"), 1)

    def test_the_invoice_carries_its_outlet_cost_centre(self):
        """A blank centre makes a real sale vanish from branch reports."""
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        profile_centre = frappe.db.get_value(
            "POS Profile", invoice.pos_profile, "cost_center")
        self.assertTrue(profile_centre, "the test till must identify its outlet")
        self.assertEqual(invoice.cost_center, profile_centre)

    def test_the_lines_carry_over(self):
        order = self.order([{"item": self.burger, "qty": 2}, {"item": self.cola, "qty": 1}])
        result = self.pay(order)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        billed = {i.item_code: i.qty for i in invoice.items}
        self.assertEqual(billed, {self.burger: 2, self.cola: 1})

    def test_the_service_charge_is_a_tax_line_not_an_item(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual(sum(t.tax_amount for t in invoice.taxes), 100)
        self.assertNotIn("service", " ".join(i.item_code.lower() for i in invoice.items))

    def test_a_take_away_bill_carries_no_tax_line(self):
        order = self.order([{"item": self.burger, "qty": 1}], order_type="Take Away")
        result = self.pay(order)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual(sum(t.tax_amount for t in invoice.taxes), 0)
        self.assertEqual(invoice.grand_total, 1000)


class TestPaymentModes(PaymentTestCase):
    def test_cash_records_one_payment_row_for_the_whole_bill(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash")
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual([(p.mode_of_payment, p.amount) for p in invoice.payments],
                         [("Cash", 1100)])

    def test_a_split_between_cash_and_card_records_both(self):
        fx.payment_mode_account(self, "Credit Card")
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash+Card", cash_amount=600, card_amount=500)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        paid = {p.mode_of_payment: p.amount for p in invoice.payments}
        self.assertEqual(paid, {"Cash": 600, "Credit Card": 500})
        self.assertEqual(sum(paid.values()), invoice.grand_total)

    def test_a_zero_leg_is_not_recorded(self):
        """Paying the whole bill by card through the split screen must not leave
        an empty Cash row on the invoice."""
        fx.payment_mode_account(self, "Credit Card")
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash+Card", cash_amount=0, card_amount=1100)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual([p.mode_of_payment for p in invoice.payments], ["Credit Card"])

    def test_change_is_recorded_when_cash_tendered_exceeds_the_bill(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash", tendered=2000)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual(invoice.get("custom_tendered_amount"), 2000)
        self.assertEqual(invoice.get("custom_change_returned"), 900)

    def test_exact_cash_records_no_change(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash", tendered=1100)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertFalse(invoice.get("custom_change_returned"))

    def test_a_nonsense_tendered_value_does_not_break_the_sale(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode="Cash", tendered="not a number")
        self.assertTrue(result["invoice_name"])


class TestDeliveryAggregatorMode(PaymentTestCase):
    """A third payment method — PickMe and its kind. Neither cash nor card, and
    nothing in the payment path may assume there are only those two."""

    def setUp(self):
        super().setUp()
        self.mode = fx.delivery_payment_mode(self, "PickMe")

    def test_the_whole_bill_is_booked_against_it(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode=self.mode)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual([(p.mode_of_payment, p.amount) for p in invoice.payments],
                         [(self.mode, 1100)])

    def test_a_split_bill_can_be_paid_by_it(self):
        order = self.order([{"item": self.burger, "qty": 1},
                            {"item": self.cola, "qty": 1}])
        result = self.split(order, [{"item": self.burger, "qty": 1}],
                            payment_mode=self.mode)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual([p.mode_of_payment for p in invoice.payments], [self.mode])

    def test_no_change_is_recorded_for_it(self):
        """Only cash has a drawer to give change from."""
        order = self.order([{"item": self.burger, "qty": 1}])
        result = self.pay(order, payment_mode=self.mode, tendered=2000)
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertFalse(invoice.get("custom_change_returned"))

    def test_the_till_offers_it(self):
        """The POS reads its buttons off the POS Profile — a mode that is not
        on the profile has no button and never reconciles at close."""
        for profile in frappe.get_all("POS Profile", pluck="name"):
            offered = [r.mode_of_payment for r in
                       frappe.get_doc("POS Profile", profile).payments]
            self.assertIn(self.mode, offered)

    def test_adding_it_again_changes_nothing(self):
        from luuvcrm import setup_doctype

        setup_doctype.add_payment_mode(self.mode)
        for profile in frappe.get_all("POS Profile", pluck="name"):
            offered = [r.mode_of_payment for r in
                       frappe.get_doc("POS Profile", profile).payments]
            self.assertEqual(offered.count(self.mode), 1)
        accounts = frappe.get_doc("Mode of Payment", self.mode).accounts
        self.assertEqual(len(accounts), len({a.company for a in accounts}))


    def test_it_can_be_taken_off_the_till_again(self):
        """remove_payment_mode drops the profile rows and nothing else — the
        Mode of Payment stays, so history that used it still resolves, and
        add_payment_mode puts the button back."""
        from luuvcrm import setup_doctype

        removed = setup_doctype.remove_payment_mode(self.mode)
        self.assertTrue(removed)
        for profile in frappe.get_all("POS Profile", pluck="name"):
            offered = [r.mode_of_payment for r in
                       frappe.get_doc("POS Profile", profile).payments]
            self.assertNotIn(self.mode, offered)
            self.assertTrue(offered, "the till must keep at least one method")
        self.assertTrue(frappe.db.exists("Mode of Payment", self.mode))
        self.assertEqual(setup_doctype.remove_payment_mode(self.mode), [])
        setup_doctype.add_payment_mode(self.mode)
        self.test_the_till_offers_it()


class TestPayingTwiceIsRefused(PaymentTestCase):
    def test_an_order_cannot_be_paid_again(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.pay(order)
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.process_payment, order_name=order.name)
        self.assertIn("already processed", str(caught.exception))

    def test_a_part_paid_order_must_go_through_split(self):
        """Once one line is billed, the full-payment path would charge for it
        twice — it refuses and sends the cashier to Split Bill."""
        order = self.order([{"item": self.burger, "qty": 2}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.process_payment, order_name=order.name)
        self.assertIn("split payments", str(caught.exception))

    def test_payment_needs_an_order_name(self):
        with self.assertRaises(frappe.ValidationError):
            self.call(api.process_payment)


class TestSplitBill(PaymentTestCase):
    def test_it_bills_only_the_chosen_lines(self):
        order = self.order([{"item": self.burger, "qty": 2}, {"item": self.cola, "qty": 2}])
        result = self.split(order, [{"item": self.cola, "qty": 2}])
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual([(i.item_code, i.qty) for i in invoice.items], [(self.cola, 2)])
        self.assertEqual(invoice.grand_total, 550, "500 of drinks plus 10% service")

    def test_it_records_what_has_been_billed(self):
        order = self.order([{"item": self.burger, "qty": 3}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        order.reload()
        self.assertEqual(int(order.items[0].paid_qty), 1)

    def test_paid_quantities_accumulate_across_splits(self):
        order = self.order([{"item": self.burger, "qty": 3}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        order.reload()
        self.assertEqual(int(order.items[0].paid_qty), 2)

    def test_the_order_closes_when_the_last_item_is_paid(self):
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        order.reload()
        self.assertEqual(order.docstatus, 0, "still open — the drink is unpaid")

        self.split(order, [{"item": self.cola, "qty": 1}])
        order.reload()
        self.assertEqual(order.docstatus, 1, "every line billed, so the bill is closed")
        self.assertEqual(order.kitchen_status, "Served")
        self.assertTrue(order.pos_invoice)

    def test_the_service_charge_follows_the_split(self):
        """Each split carries service charge on its own dine-in lines, so the
        parts add up to what the whole bill would have been."""
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        first = self.split(order, [{"item": self.burger, "qty": 1}])
        second = self.split(order, [{"item": self.cola, "qty": 1}])
        total = (frappe.db.get_value("POS Invoice", first["invoice_name"], "grand_total")
                 + frappe.db.get_value("POS Invoice", second["invoice_name"], "grand_total"))
        self.assertEqual(total, 1375, "the two halves must equal the whole bill")

    def test_a_parcelled_line_is_split_without_service_charge(self):
        order = self.order([{"item": self.burger, "qty": 1},
                            {"item": self.cola, "qty": 1, "takeaway": 1}])
        result = self.split(order, [{"item": self.cola, "qty": 1}])
        invoice = frappe.get_doc("POS Invoice", result["invoice_name"])
        self.assertEqual(invoice.grand_total, 250, "a parcel carries no service charge")


class TestSplitBillRefusals(PaymentTestCase):
    """The `paid_qty` guard. Each of these, if it passed, would either bill a
    customer twice or hand out food nobody paid for."""

    def test_billing_more_than_the_order_has_is_refused(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [{"item": self.burger, "qty": 3}])
        self.assertIn("left to bill", str(caught.exception))

    def test_a_second_split_cannot_exceed_what_is_left(self):
        order = self.order([{"item": self.burger, "qty": 3}])
        self.split(order, [{"item": self.burger, "qty": 2}])
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [{"item": self.burger, "qty": 2}])
        self.assertIn("left to bill", str(caught.exception))

    def test_the_last_line_paid_closes_the_order_against_further_billing(self):
        """Belt and braces: even if the remaining check were wrong, an order
        that has been fully billed is submitted, and a submitted order is not
        open for another split."""
        order = self.order([{"item": self.burger, "qty": 2}])
        self.split(order, [{"item": self.burger, "qty": 2}])
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [{"item": self.burger, "qty": 1}])
        self.assertIn("not open", str(caught.exception))

    def test_two_lines_for_the_same_item_are_counted_together(self):
        """Sending the same item twice in one request must not slip past the
        remaining check by being validated one line at a time."""
        order = self.order([{"item": self.burger, "qty": 2}])
        with self.assertRaises(frappe.ValidationError):
            self.split(order, [{"item": self.burger, "qty": 2},
                               {"item": self.burger, "qty": 1}])

    def test_an_item_not_on_the_order_is_refused(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [{"item": self.cola, "qty": 1}])
        self.assertIn("not on this order", str(caught.exception))

    def test_an_empty_selection_is_refused(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [])
        self.assertIn("at least one item", str(caught.exception))

    def test_a_zero_quantity_line_is_not_a_selection(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        with self.assertRaises(frappe.ValidationError):
            self.split(order, [{"item": self.burger, "qty": 0}])

    def test_a_closed_order_cannot_be_split_again(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.split(order, [{"item": self.burger, "qty": 1}])
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.split(order, [{"item": self.burger, "qty": 1}])
        self.assertIn("not open", str(caught.exception))


class TestFullyPaid(FrappeTestCase):
    """`_order_fully_paid` decides when a bill closes."""

    def test_an_order_with_nothing_on_it_is_not_paid(self):
        self.assertFalse(api._order_fully_paid({"items": []}),
                         "an empty order must not close itself")

    def test_it_is_paid_only_when_every_line_is(self):
        order = frappe._dict(items=[frappe._dict(qty=2, paid_qty=2),
                                    frappe._dict(qty=1, paid_qty=0)])
        self.assertFalse(api._order_fully_paid(order))
        order["items"][1].paid_qty = 1
        self.assertTrue(api._order_fully_paid(order))

    def test_over_billing_still_counts_as_paid(self):
        order = frappe._dict(items=[frappe._dict(qty=1, paid_qty=2)])
        self.assertTrue(api._order_fully_paid(order))
