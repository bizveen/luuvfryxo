"""Inter-restaurant kitchen request workflow and isolation."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import roles
from luuvcrm.api import branch_requests
from luuvcrm.tests import fixtures as fx
from luuvcrm.tests.test_branch import _permit, _restaurant, _user


class TestBranchKitchenRequests(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.requester_branch = _restaurant(self, "Kitchen-Requester")
        self.supplier_branch = _restaurant(self, "Kitchen-Supplier")
        self.other_branch = _restaurant(self, "Kitchen-Other")
        self.item = fx.item(self, "BRANCH-SAUCE")

        self.requester = _user(self, "kitchen-requester", roles.KITCHEN_STAFF)
        self.supplier = _user(self, "kitchen-supplier", roles.KITCHEN_STAFF)
        self.other = _user(self, "kitchen-other", roles.KITCHEN_STAFF)
        self.waiter = _user(self, "kitchen-request-waiter", roles.WAITER)
        _permit(self, self.requester, self.requester_branch)
        _permit(self, self.supplier, self.supplier_branch)
        _permit(self, self.other, self.other_branch)

        self.previous_user = frappe.session.user
        self.addCleanup(frappe.set_user, self.previous_user)
        self.requests = []
        self.addCleanup(self._cleanup_requests)

    def _cleanup_requests(self):
        frappe.set_user("Administrator")
        for name in self.requests:
            if frappe.db.exists("Branch Kitchen Request", name):
                frappe.delete_doc("Branch Kitchen Request", name, force=True,
                                  ignore_permissions=True)

    def as_user(self, user):
        frappe.set_user(user)

    def create(self, **overrides):
        self.as_user(self.requester)
        values = {
            "supplying_restaurant": self.supplier_branch,
            "items": json.dumps([{"item": self.item, "qty": 3}]),
            "priority": "Urgent",
            "notes": "Pack cold",
        }
        values.update(overrides)
        result = branch_requests.create_branch_kitchen_request(**values)
        self.requests.append(result["name"])
        return result["name"]

    def transition(self, user, name, status):
        self.as_user(user)
        return branch_requests.update_branch_kitchen_request_status(name, status)

    def test_request_is_stamped_and_visible_to_both_sides_only(self):
        name = self.create()
        doc = frappe.get_doc("Branch Kitchen Request", name)
        self.assertEqual(doc.requesting_restaurant, self.requester_branch)
        self.assertEqual(doc.supplying_restaurant, self.supplier_branch)
        self.assertEqual(doc.status, "Requested")
        self.assertEqual(doc.requested_by, self.requester)
        self.assertEqual(doc.items[0].item, self.item)
        self.assertEqual(doc.items[0].qty, 3)
        self.assertEqual([row.status for row in doc.status_history], ["Requested"])

        self.as_user(self.requester)
        outgoing = branch_requests.get_branch_kitchen_requests()["requests"]
        self.assertEqual(outgoing[0]["direction"], "outgoing")
        self.as_user(self.supplier)
        incoming = branch_requests.get_branch_kitchen_requests()["requests"]
        self.assertEqual(incoming[0]["direction"], "incoming")
        self.as_user(self.other)
        self.assertNotIn(name, [row.name for row in
                               branch_requests.get_branch_kitchen_requests()["requests"]])

    def test_status_follows_the_two_branch_handoff(self):
        name = self.create()
        for status in ("Accepted", "Preparing", "Ready", "Dispatched"):
            self.transition(self.supplier, name, status)
        self.transition(self.requester, name, "Received")

        doc = frappe.get_doc("Branch Kitchen Request", name)
        self.assertEqual(doc.status, "Received")
        self.assertEqual(
            [row.status for row in doc.status_history],
            ["Requested", "Accepted", "Preparing", "Ready", "Dispatched", "Received"],
        )

    def test_wrong_branch_and_status_jump_are_refused(self):
        name = self.create()
        self.as_user(self.requester)
        with self.assertRaises(frappe.PermissionError):
            branch_requests.update_branch_kitchen_request_status(name, "Accepted")
        self.as_user(self.supplier)
        with self.assertRaises(frappe.ValidationError):
            branch_requests.update_branch_kitchen_request_status(name, "Ready")

    def test_requester_can_cancel_before_preparation(self):
        name = self.create()
        self.transition(self.requester, name, "Cancelled")
        self.assertEqual(frappe.db.get_value("Branch Kitchen Request", name, "status"),
                         "Cancelled")

    def test_waiter_cannot_open_the_cross_branch_request_api(self):
        self.as_user(self.waiter)
        with self.assertRaises(frappe.PermissionError):
            branch_requests.get_branch_kitchen_requests()

    def test_generic_doctype_access_is_read_only_for_kitchen_staff(self):
        self.assertTrue(frappe.has_permission(
            "Branch Kitchen Request", "read", user=self.requester))
        self.assertFalse(frappe.has_permission(
            "Branch Kitchen Request", "write", user=self.requester))
        self.assertFalse(frappe.has_permission(
            "Branch Kitchen Request", "read", user=self.waiter))


class TestBranchKitchenRequestScreen(FrappeTestCase):
    def test_the_three_new_doctypes_are_standard_migrated_schema(self):
        for doctype in (
            "Branch Kitchen Request Item",
            "Branch Kitchen Request Status",
            "Branch Kitchen Request",
        ):
            with self.subTest(doctype=doctype):
                self.assertTrue(frappe.db.exists("DocType", doctype))
                self.assertFalse(frappe.db.get_value("DocType", doctype, "custom"))

    def test_kitchen_has_request_tab_form_timeline_and_polling(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "kitchen.html"))
        for marker in (
            "Branch Requests", "New Branch Kitchen Request", "Status history",
            "create_branch_kitchen_request", "update_branch_kitchen_request_status",
            "loadBranchRequests", "refreshAll()",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_the_kitchen_template_renders_the_tab_only_for_allowed_roles(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "kitchen.html"))
        common = {
            "brand_name": "Test Restaurant",
            "csrf_token": "test",
            "current_user_full": "Kitchen User",
            "restaurant_name": "Kurunegala",
        }
        allowed = frappe.render_template(html, {**common, "can_branch_requests": True})
        denied = frappe.render_template(html, {**common, "can_branch_requests": False})
        self.assertIn('id="tabSupply"', allowed)
        self.assertNotIn('id="tabSupply"', denied)
