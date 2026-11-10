"""Customer device registration and Firebase delivery boundaries."""

from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
import requests
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
    @patch("luuvcrm.api.push.frappe.db.exists", return_value=True)
    @patch("luuvcrm.api.push.frappe.db.get_value")
    def test_order_ready_delivery_is_queued_after_commit(self, get_value, _exists, enqueue):
        get_value.return_value = self.MOBILE

        self.assertTrue(api.queue_order_ready_push("POS-ORDER-0001"))
        enqueue.assert_called_once_with(
            "luuvcrm.api.push.send_order_ready_push",
            queue="short",
            enqueue_after_commit=True,
            order_name="POS-ORDER-0001",
        )


class TestPushSafety(TestCase):
    """Push failures and branch authorization, with no database or delivery."""

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.branch.viewing", return_value="Kurunegala")
    @patch("luuvcrm.api.push.branch.available_restaurants", return_value=[])
    @patch("luuvcrm.api.push.roles.is_owner", return_value=False)
    @patch("luuvcrm.api.push.roles.require_manager")
    def test_unassigned_manager_cannot_broadcast_to_implicit_branch(
            self, _require, _owner, _allowed, _viewing, enqueue):
        with self.assertRaises(frappe.PermissionError):
            api.queue_customer_push_broadcast(
                kind="offer", title="Offer", body="Details", restaurant="")
        enqueue.assert_not_called()

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.branch.viewing", return_value="Kurunegala")
    @patch("luuvcrm.api.push.branch.available_restaurants", return_value=["Luuv Fryxo"])
    @patch("luuvcrm.api.push.roles.is_owner", return_value=False)
    @patch("luuvcrm.api.push.roles.require_manager")
    def test_implicit_branch_must_be_one_of_the_managers_assignments(
            self, _require, _owner, _allowed, _viewing, enqueue):
        with self.assertRaises(frappe.PermissionError):
            api.queue_customer_push_broadcast(
                kind="offer", title="Offer", body="Details", restaurant="")
        enqueue.assert_not_called()

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push._enabled_devices", return_value=[])
    @patch("luuvcrm.api.push.branch.viewing", return_value="Kurunegala")
    @patch("luuvcrm.api.push.branch.available_restaurants", return_value=["Kurunegala"])
    @patch("luuvcrm.api.push.roles.is_owner", return_value=False)
    @patch("luuvcrm.api.push.roles.require_manager")
    def test_assigned_manager_broadcast_is_scoped_to_their_implicit_branch(
            self, _require, _owner, _allowed, _viewing, devices, enqueue):
        result = api.queue_customer_push_broadcast(
            kind="offer", title="Offer", body="Details", restaurant="")
        self.assertEqual(result["restaurant"], "Kurunegala")
        devices.assert_called_once_with({"restaurant": "Kurunegala"})
        self.assertEqual(enqueue.call_args.kwargs["restaurant"], "Kurunegala")

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push._enabled_devices", return_value=[])
    @patch("luuvcrm.api.push.roles.is_owner", return_value=True)
    @patch("luuvcrm.api.push.roles.require_manager")
    def test_owner_can_still_broadcast_to_all_branches(
            self, _require, _owner, devices, enqueue):
        result = api.queue_customer_push_broadcast(
            kind="offer", title="Offer", body="Details", restaurant="")
        self.assertEqual(result["restaurant"], "All")
        devices.assert_called_once_with({})
        self.assertEqual(enqueue.call_args.kwargs["restaurant"], "")

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.frappe.db.get_value")
    @patch("luuvcrm.api.push.frappe.db.exists", return_value=False)
    def test_ready_skips_sites_without_push_schema(self, exists, get_value, enqueue):
        self.assertFalse(api.queue_order_ready_push("MOCK-ORDER"))
        exists.assert_called_once_with("DocType", api.PUSH_DOCTYPE)
        get_value.assert_not_called()
        enqueue.assert_not_called()

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.frappe.db.get_value", return_value="0770000841")
    @patch("luuvcrm.api.push.frappe.db.exists", side_effect=[True, False])
    def test_ready_skips_customers_without_enabled_devices(self, exists, _value, enqueue):
        self.assertFalse(api.queue_order_ready_push("MOCK-ORDER"))
        exists.assert_called_with(api.PUSH_DOCTYPE, {"mobile": "0770000841", "enabled": 1})
        enqueue.assert_not_called()

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.frappe.db.get_value", return_value="")
    @patch("luuvcrm.api.push.frappe.db.exists", return_value=True)
    def test_ready_skips_orders_without_a_phone(self, exists, _value, enqueue):
        self.assertFalse(api.queue_order_ready_push("MOCK-ORDER"))
        exists.assert_called_once_with("DocType", api.PUSH_DOCTYPE)
        enqueue.assert_not_called()

    @patch("luuvcrm.api.push.frappe.enqueue")
    @patch("luuvcrm.api.push.frappe.db.get_value", return_value="0770000841")
    @patch("luuvcrm.api.push.frappe.db.exists", return_value=True)
    def test_ready_with_enabled_device_is_queued_after_commit(self, _exists, _value, enqueue):
        self.assertTrue(api.queue_order_ready_push("MOCK-ORDER"))
        enqueue.assert_called_once_with(
            "luuvcrm.api.push.send_order_ready_push", queue="short",
            enqueue_after_commit=True, order_name="MOCK-ORDER")

    @patch("luuvcrm.api.push.frappe.log_error")
    @patch("luuvcrm.api.push.requests.post")
    @patch("luuvcrm.api.push._fcm_context", return_value=("mock-secret", "mock-project"))
    @patch("luuvcrm.api.push._enabled_devices")
    def test_one_timeout_does_not_stop_delivery_to_later_devices(
            self, devices, _context, post, log):
        devices.return_value = [
            frappe._dict(name="hash-one", device_token="mock-device-one"),
            frappe._dict(name="hash-two", device_token="mock-device-two"),
        ]
        post.side_effect = [
            requests.Timeout("mock-secret mock-device-one"),
            Mock(ok=True, status_code=200, text=""),
        ]
        result = api.send_customer_push_broadcast("offer", "Offer", "Details")
        self.assertEqual(result, {"sent": 1, "failed": 1})
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("mock-secret", str(log.call_args))
        self.assertNotIn("mock-device-one", str(log.call_args))
        self.assertIn("Timeout", log.call_args.kwargs["message"])

    @patch("luuvcrm.api.push.frappe.log_error")
    @patch("luuvcrm.api.push.requests.post")
    def test_failure_response_contents_are_not_logged(self, post, log):
        post.return_value = Mock(
            ok=False, status_code=500, text="mock-secret mock-device-one")
        self.assertFalse(api._send_one(
            frappe._dict(name="hash-one", device_token="mock-device-one"),
            "Offer", "Details", {}, ("mock-secret", "mock-project")))
        self.assertNotIn("mock-secret", str(log.call_args))
        self.assertNotIn("mock-device-one", str(log.call_args))

    @patch("luuvcrm.api.push.frappe.log_error")
    @patch("luuvcrm.api.push._fcm_context", side_effect=RuntimeError("mock-secret"))
    @patch("luuvcrm.api.push._enabled_devices", return_value=[Mock()])
    def test_credentials_failure_is_counted_without_logging_its_contents(
            self, _devices, _context, log):
        self.assertEqual(api.send_customer_push("0770000841", "Offer", "Details"),
                         {"sent": 0, "failed": 1})
        self.assertEqual(api.send_customer_push_broadcast("offer", "Offer", "Details"),
                         {"sent": 0, "failed": 1})
        self.assertEqual(log.call_count, 2)
        self.assertNotIn("mock-secret", str(log.call_args_list))
