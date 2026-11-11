"""Guest customer-app notification registration and targeting."""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class TestGuestOrderPush(FrappeTestCase):
    MOBILE = "0770000842"

    def setUp(self):
        super().setUp()
        self.device_token = "fcm-guest-" + ("b" * 80)
        self.item = fx.item(self, "PUSH-GUEST")
        fx.item_price(self, self.item, 500)
        self.orders = []

    def tearDown(self):
        for name in frappe.get_all(
                api.PUSH_DOCTYPE,
                filters={"device_token": self.device_token},
                pluck="name"):
            frappe.delete_doc(api.PUSH_DOCTYPE, name, force=True,
                              ignore_permissions=True)
        for name in self.orders:
            if frappe.db.exists("POS Order", name):
                frappe.delete_doc("POS Order", name, force=True,
                                  ignore_permissions=True)
        super().tearDown()

    def guest_order(self):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict({
            "items": frappe.as_json([{"item": self.item, "qty": 1}]),
            "order_type": "Take Away",
        })
        try:
            result = api.send_to_kitchen()
        finally:
            frappe.local.form_dict = previous
        name = result["name"] if isinstance(result, dict) else result
        self.orders.append(name)
        frappe.db.set_value("POS Order", name, {
            "mobile": self.MOBILE,
            "order_source": "Online",
            "waiter_name": "CUS",
            "kitchen_status": "Draft",
        })
        return frappe.get_doc("POS Order", name)

    def register(self, order, ticket=None):
        return api.customer_register_guest_order_push(
            order_name=order.name,
            guest_ticket=ticket or api.guest_order_push_ticket(order.name),
            device_token=self.device_token,
            platform="android",
        )

    def test_installation_is_bound_to_only_the_order_it_created(self):
        order = self.guest_order()

        result = self.register(order)

        self.assertTrue(result["success"])
        row = frappe.db.get_value(
            api.PUSH_DOCTYPE,
            api._guest_token_hash(order.name, self.device_token),
            ["mobile", "token_scope", "order_name", "enabled"],
            as_dict=True,
        )
        self.assertEqual(row.mobile, self.MOBILE)
        self.assertEqual(row.token_scope, api.GUEST_ORDER_SCOPE)
        self.assertEqual(row.order_name, order.name)
        self.assertEqual(row.enabled, 1)

    def test_registration_rejects_a_ticket_for_another_order(self):
        order = self.guest_order()

        result = self.register(
            order, ticket=api.guest_order_push_ticket("POS-SOMEONE-ELSE"))

        self.assertFalse(result["success"])
        self.assertFalse(frappe.db.exists(
            api.PUSH_DOCTYPE,
            api._guest_token_hash(order.name, self.device_token),
        ))

    def test_ready_delivery_finds_the_guest_device_without_login(self):
        order = self.guest_order()
        self.register(order)

        devices = api._order_ready_devices(order.name, order.mobile)

        self.assertEqual([device.device_token for device in devices],
                         [self.device_token])
