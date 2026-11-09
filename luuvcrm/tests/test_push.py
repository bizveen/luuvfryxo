"""Customer device registration and Firebase delivery boundaries."""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class PushTestCase(FrappeTestCase):
    MOBILE = "0770000841"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE)
        self.customer_token = fx.customer_token(self, self.MOBILE)
        self.device_token = "fcm-" + ("a" * 80)

    def tearDown(self):
        digest = api._token_hash(self.device_token)
        if frappe.db.exists(api.PUSH_DOCTYPE, digest):
            frappe.delete_doc(api.PUSH_DOCTYPE, digest, force=True,
                              ignore_permissions=True)
        super().tearDown()


class TestPushRegistration(PushTestCase):
    def test_customer_registers_their_installation(self):
        result = api.customer_register_push_token(
            customer_token=self.customer_token,
            device_token=self.device_token,
            platform="android",
            app_version="1.2.3",
        )

        self.assertTrue(result["success"])
        row = frappe.db.get_value(
            api.PUSH_DOCTYPE,
            api._token_hash(self.device_token),
            ["mobile", "platform", "app_version", "enabled"],
            as_dict=True,
        )
        self.assertEqual(row.mobile, self.MOBILE)
        self.assertEqual(row.platform, "Android")
        self.assertEqual(row.app_version, "1.2.3")
        self.assertEqual(row.enabled, 1)

    def test_a_made_up_customer_token_cannot_register(self):
        result = api.customer_register_push_token(
            customer_token="not-a-customer-session",
            device_token=self.device_token,
            platform="android",
        )
        self.assertFalse(result["success"])
        self.assertFalse(frappe.db.exists(
            api.PUSH_DOCTYPE, api._token_hash(self.device_token)))

    def test_sign_out_disables_only_the_customers_device(self):
        api.customer_register_push_token(
            customer_token=self.customer_token,
            device_token=self.device_token,
            platform="ios",
        )
        result = api.customer_unregister_push_token(
            customer_token=self.customer_token,
            device_token=self.device_token,
        )

        self.assertTrue(result["success"])
        self.assertEqual(frappe.db.get_value(
            api.PUSH_DOCTYPE, api._token_hash(self.device_token), "enabled"), 0)


class TestFirebaseDelivery(PushTestCase):
    @patch("luuvcrm.api.push.requests.post")
    def test_http_v1_payload_uses_the_matching_notification_channel(self, post):
        post.return_value = Mock(ok=True, status_code=200, text="")
        device = frappe._dict(name=api._token_hash(self.device_token),
                              device_token=self.device_token)

        sent = api._send_one(
            device,
            "Ready",
            "Your order is prepared",
            {"type": "order_ready", "route": "/cus?tab=orders"},
            ("access-token", "luuv-test"),
        )

        self.assertTrue(sent)
        request = post.call_args.kwargs
        self.assertEqual(request["json"]["message"]["android"]
                         ["notification"]["channel_id"], "order_updates")
        self.assertEqual(request["json"]["message"]["token"], self.device_token)
        self.assertNotIn(self.device_token, str(request["headers"]))

    @patch("luuvcrm.api.push.frappe.log_error")
    @patch("luuvcrm.api.push.requests.post")
    def test_fcm_unregistered_response_disables_a_dead_token(self, post, _log):
        api.customer_register_push_token(
            customer_token=self.customer_token,
            device_token=self.device_token,
            platform="android",
        )
        post.return_value = Mock(
            ok=False,
            status_code=404,
            text='{"error":{"status":"UNREGISTERED"}}',
        )
        device = frappe._dict(name=api._token_hash(self.device_token),
                              device_token=self.device_token)

        self.assertFalse(api._send_one(
            device, "Offer", "Expired device", {"type": "offer"},
            ("access-token", "luuv-test")))
        self.assertEqual(frappe.db.get_value(
            api.PUSH_DOCTYPE, device.name, "enabled"), 0)

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.frappe.db.get_value")
    def test_order_ready_delivery_is_queued_after_commit(self, get_value, enqueue):
        get_value.return_value = self.MOBILE

        self.assertTrue(api.queue_order_ready_push("POS-ORDER-0001"))
        enqueue.assert_called_once_with(
            "luuvcrm.api.push.send_order_ready_push",
            queue="short",
            enqueue_after_commit=True,
            order_name="POS-ORDER-0001",
        )
