import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api


class TestCustomerOffers(FrappeTestCase):
    NAME = "_LFT-Buy Five Preview"

    def setUp(self):
        super().setUp()
        if frappe.db.exists("Customer Offer", self.NAME):
            frappe.delete_doc("Customer Offer", self.NAME, force=True,
                              ignore_permissions=True, delete_permanently=True)
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        self.offer = frappe.get_doc({
            "doctype": "Customer Offer",
            "offer_name": self.NAME,
            "title": "Buy 5, get 1 FREE",
            "description": "Test campaign",
            "offer_type": "Buy X Get Y Free",
            "buy_quantity": 5,
            "free_quantity": 1,
            "promo_code": "LFT5PLUS1",
            "restaurant": restaurant,
            "active": 1,
        }).insert(ignore_permissions=True)
        self.restaurant = restaurant
        self.addCleanup(self.drop_offer)

    def drop_offer(self):
        if frappe.db.exists("Customer Offer", self.NAME):
            frappe.delete_doc("Customer Offer", self.NAME, force=True,
                              ignore_permissions=True, delete_permanently=True)

    def call(self, endpoint, **values):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(values)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def test_active_offer_is_published_to_the_customer_app(self):
        result = self.call(api.customer_active_offers,
                           restaurant=self.restaurant)
        row = next(o for o in result["offers"] if o["name"] == self.NAME)
        self.assertEqual(row["title"], "Buy 5, get 1 FREE")
        self.assertEqual(row["buy_quantity"], 5)
        self.assertNotIn("owner", row, "the public API exposes display fields only")

    def test_paused_offer_is_not_published(self):
        frappe.db.set_value("Customer Offer", self.NAME, "active", 0)
        result = self.call(api.customer_active_offers,
                           restaurant=self.restaurant)
        self.assertNotIn(self.NAME, [o["name"] for o in result["offers"]])

    def test_buy_five_is_priced_on_the_server_and_adds_one_free(self):
        result = api.prepare_customer_offer(
            self.NAME, "0771234567", self.restaurant,
            [{"item": "MENU-TEST", "qty": 5, "rate": 400}], 2000,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["free_item"], "MENU-TEST")
        self.assertEqual(result["free_quantity"], 1)
        self.assertEqual(result["benefit_amount"], 400)

    def test_buy_five_rejects_a_cart_that_does_not_qualify(self):
        result = api.prepare_customer_offer(
            self.NAME, "0771234567", self.restaurant,
            [{"item": "MENU-TEST", "qty": 4, "rate": 400}], 1600,
        )
        self.assertFalse(result["applied"])
        self.assertIn("1 more", result["error"])

    def test_manager_can_create_an_offer_from_the_app(self):
        previous_user = frappe.session.user
        frappe.set_user("Administrator")
        created = "_LFT-Manager Offer"
        self.addCleanup(lambda: frappe.db.exists("Customer Offer", created) and
                        frappe.delete_doc("Customer Offer", created, force=True,
                                          ignore_permissions=True,
                                          delete_permanently=True))
        try:
            result = self.call(
                api.manager_save_offer,
                title=created,
                offer_name=created,
                description="Created from manager",
                offer_type="Announcement",
                active="1",
            )
        finally:
            frappe.set_user(previous_user)
        self.assertTrue(result["success"])
        self.assertTrue(frappe.db.exists("Customer Offer", created))

    def test_guest_cannot_use_manager_offer_tools(self):
        previous_user = frappe.session.user
        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                self.call(api.manager_get_offers)
        finally:
            frappe.set_user(previous_user)
