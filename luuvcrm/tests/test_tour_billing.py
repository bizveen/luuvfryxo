"""Booking a bill under a Tour Company at the till.

No money changes: the guest pays the full price whether or not a company is
picked. What changes is that the invoice remembers the company and the company's
discount as it was that day, so the statement can be made later from the invoices
alone. The tests hold to both halves — the link is kept, and the total is not
touched.
"""

import json

import frappe

from luuvcrm import api, roles
from luuvcrm.tests import fixtures as fx
from luuvcrm.tests.test_payments import PaymentTestCase

NAME = "_LFT Blue Wave"


class TourBillingTestCase(PaymentTestCase):
    def setUp(self):
        super().setUp()
        if not frappe.db.has_column("POS Invoice", "tour_company"):
            self.skipTest("POS Invoice has no tour_company field on this site")
        self.company(NAME, 10)

    def company(self, name, discount=10, disabled=0):
        doc = frappe.get_doc({"doctype": "Tour Company", "company_name": name,
                              "discount_percent": discount, "disabled": disabled,
                              "items": json.dumps([self.burger])})
        doc.insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Tour Company", doc.name, force=True, ignore_permissions=True)
        return doc

    def stamp(self, invoice_name):
        return frappe.db.get_value("POS Invoice", invoice_name, ["tour_company", "tour_discount", "grand_total"],
                                   as_dict=True)

    def invoices(self):
        return frappe.db.count("POS Invoice")


class TestPayingUnderACompany(TourBillingTestCase):
    def test_the_invoice_keeps_the_company_and_its_discount(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        got = self.stamp(self.pay(order, tour_company=NAME)["invoice_name"])
        self.assertEqual((got.tour_company, got.tour_discount), (NAME, 10))

    def test_the_guest_still_pays_the_full_price(self):
        with_company = self.stamp(self.pay(self.order([{"item": self.burger, "qty": 2}]),
                                           tour_company=NAME)["invoice_name"])
        without = self.stamp(self.pay(self.order([{"item": self.burger, "qty": 2}]))["invoice_name"])
        self.assertEqual(with_company.grand_total, without.grand_total)
        self.assertEqual(with_company.grand_total, 2200)

    def test_no_company_is_no_link(self):
        got = self.stamp(self.pay(self.order([{"item": self.burger, "qty": 1}]))["invoice_name"])
        self.assertFalse(got.tour_company)

    def test_a_blank_company_is_no_company(self):
        got = self.stamp(self.pay(self.order([{"item": self.burger, "qty": 1}]), tour_company="  ")["invoice_name"])
        self.assertFalse(got.tour_company)

    def test_the_discount_is_the_one_on_the_day_not_the_one_today(self):
        invoice = self.pay(self.order([{"item": self.burger, "qty": 1}]), tour_company=NAME)["invoice_name"]
        frappe.db.set_value("Tour Company", NAME, "discount_percent", 25)
        self.assertEqual(self.stamp(invoice).tour_discount, 10)

    def test_the_company_is_in_the_activity_trail(self):
        result = self.pay(self.order([{"item": self.burger, "qty": 1}]), tour_company=NAME)
        row = frappe.db.get_value("LF Activity Log", {"bill_no": result["invoice_name"]}, "detail")
        self.assertIn(NAME, row or "")


class TestTheOtherWaysToPay(TourBillingTestCase):
    def test_a_walk_in_sale_can_be_booked_under_a_company(self):
        result = self.call(api.place_order, items=json.dumps([{"item": self.burger, "qty": 1}]),
                           order_type="Take Away", payment_mode="Cash", tour_company=NAME)
        fx.drop_invoice(self, result["invoice_name"])
        self._orders.append(result["name"])
        self.assertEqual(self.stamp(result["invoice_name"]).tour_company, NAME)

    def test_a_split_bill_can_be_booked_under_a_company(self):
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        got = self.stamp(self.split(order, [{"item": self.cola, "qty": 1}], tour_company=NAME)["invoice_name"])
        self.assertEqual((got.tour_company, got.tour_discount), (NAME, 10))

    def test_each_half_of_a_split_can_be_a_different_company_or_none(self):
        self.company("_LFT Green Wave", 15)
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        a = self.stamp(self.split(order, [{"item": self.burger, "qty": 1}], tour_company=NAME)["invoice_name"])
        b = self.stamp(self.split(order, [{"item": self.cola, "qty": 1}])["invoice_name"])
        self.assertEqual(a.tour_company, NAME)
        self.assertFalse(b.tour_company)


class TestWhatIsRefused(TourBillingTestCase):
    def refused(self, name, via):
        before = self.invoices()
        with self.assertRaises(frappe.ValidationError):
            via(name)
        self.assertEqual(self.invoices(), before, "a refused payment must not leave an invoice")

    def test_an_unknown_company(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.refused("_LFT Nobody", lambda n: self.pay(order, tour_company=n))
        self.pay(order)                       # and the order is still payable

    def test_a_removed_company(self):
        self.company("_LFT Gone", 10, disabled=1)
        order = self.order([{"item": self.burger, "qty": 1}])
        self.refused("_LFT Gone", lambda n: self.pay(order, tour_company=n))

    def test_the_same_for_a_walk_in_and_a_split(self):
        self.company("_LFT Gone2", 10, disabled=1)
        self.refused("_LFT Gone2", lambda n: self.call(
            api.place_order, items=json.dumps([{"item": self.burger, "qty": 1}]),
            order_type="Take Away", payment_mode="Cash", tour_company=n))
        order = self.order([{"item": self.burger, "qty": 1}])
        self.refused("_LFT Gone2", lambda n: self.split(order, [{"item": self.burger, "qty": 1}], tour_company=n))


class TestWhatTheTillIsOffered(TourBillingTestCase):
    def as_role(self, role, tag):
        email = f"lf-test-tour-{tag}@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Tour Test",
                            "send_welcome_email": 0, "roles": [{"role": role}]}).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def test_a_cashier_gets_the_active_names_and_nothing_else(self):
        self.company("_LFT Gone3", 10, disabled=1)
        self.as_role(roles.CASHIER, "cashier")
        got = api.get_tour_company_options()
        self.assertIn(NAME, got["companies"])
        self.assertNotIn("_LFT Gone3", got["companies"])
        self.assertEqual(list(got), ["companies"])
        self.assertTrue(all(isinstance(n, str) for n in got["companies"]), "names only, no discount")

    def test_a_manager_gets_them_too(self):
        self.as_role(roles.RESTAURANT_MANAGER, "manager")
        self.assertIn(NAME, api.get_tour_company_options()["companies"])

    def test_someone_with_no_till_role_is_refused(self):
        self.as_role(roles.WAITER, "waiter")
        with self.assertRaises(frappe.PermissionError):
            api.get_tour_company_options()
