"""Read-only regressions for the staged multi-restaurant release."""
import unittest
from unittest.mock import Mock, patch

import frappe
from luuvcrm import api, branch
from luuvcrm.api import branch_requests, purchases


class TestReleaseGuards(unittest.TestCase):
    def test_staff_labels_preserve_internal_ids(self):
        records = {
            "Luuv Fryxo": frappe._dict(restaurant_name="Rangiri Luuv", city="Dambulla"),
            "Kurunegala": frappe._dict(restaurant_name="Luuv Fryxo", city="Kurunegala"),
        }
        with patch.object(frappe, "get_doc", side_effect=lambda dt, name: records[name]):
            self.assertEqual(branch.display_label("Luuv Fryxo"), "Rangiri Luuv — Dambulla")
            self.assertEqual(branch.display_label("Kurunegala"), "Luuv Fryxo — Kurunegala")

    def test_direct_lookup_denies_other_branch(self):
        with patch.object(frappe.db, "exists", return_value=True), \
                patch.object(frappe.db, "has_column", return_value=True), \
                patch.object(frappe.db, "get_value", return_value="Kurunegala"), \
                patch.object(branch, "_permitted_restaurants", return_value=["Luuv Fryxo"]):
            with self.assertRaises(frappe.PermissionError):
                branch.require_access("POS Order", "TEST", user="staff@example.com")

    def test_direct_lookup_allows_own_branch(self):
        with patch.object(frappe.db, "exists", return_value=True), \
                patch.object(frappe.db, "has_column", return_value=True), \
                patch.object(frappe.db, "get_value", return_value="Luuv Fryxo"), \
                patch.object(branch, "_permitted_restaurants", return_value=["Luuv Fryxo"]):
            self.assertEqual(branch.require_access("POS Order", "TEST", user="staff@example.com"), "Luuv Fryxo")

    def test_tills_are_filtered_by_restaurant(self):
        with patch.object(frappe, "get_all", return_value=["Dambulla", "Kurunegala"]), \
                patch.object(branch, "_permitted_restaurants", return_value=["Luuv Fryxo"]), \
                patch.object(branch, "_restaurant_for_profile", side_effect=lambda p: {"Dambulla": "Luuv Fryxo", "Kurunegala": "Kurunegala"}[p]):
            self.assertEqual(branch.accessible_pos_profiles("staff@example.com"), ["Dambulla"])

    def test_requester_cannot_accept_own_request(self):
        doc = Mock(status="Requested", requesting_restaurant="A", supplying_restaurant="B")
        with patch.object(branch_requests, "_require_access"), \
                patch.object(branch_requests, "_request_doc_for_active_branch", return_value=(doc, "A")):
            with self.assertRaises(frappe.PermissionError):
                branch_requests.update_branch_kitchen_request_status("TEST", "Accepted")
        doc.save.assert_not_called()

    def test_supplier_accept_records_history(self):
        doc = Mock(status="Requested", requesting_restaurant="A", supplying_restaurant="B")
        doc.name = "TEST"
        with patch.object(branch_requests, "_require_access"), \
                patch.object(branch_requests, "_request_doc_for_active_branch", return_value=(doc, "B")):
            result = branch_requests.update_branch_kitchen_request_status("TEST", "Accepted")
        self.assertEqual(result["status"], "Accepted")
        self.assertEqual(doc.append.call_args.args[1]["status"], "Accepted")
        doc.save.assert_called_once()

    def test_request_cannot_skip_to_received(self):
        doc = Mock(status="Requested", requesting_restaurant="A", supplying_restaurant="B")
        with patch.object(branch_requests, "_require_access"), \
                patch.object(branch_requests, "_request_doc_for_active_branch", return_value=(doc, "A")):
            with self.assertRaises(frappe.ValidationError):
                branch_requests.update_branch_kitchen_request_status("TEST", "Received")
        doc.save.assert_not_called()

    def test_purchase_date_preserves_entered_date(self):
        doc = frappe._dict(purchase_date="2026-10-20", requested_at="2026-10-22 10:00:00")
        purchases.ensure_purchase_date(doc)
        self.assertEqual(doc.purchase_date, "2026-10-20")
        doc.purchase_date = None
        purchases.ensure_purchase_date(doc)
        self.assertEqual(str(doc.purchase_date), "2026-10-22")

    def test_new_and_existing_api_paths_coexist(self):
        for name in ("create_branch_kitchen_request", "get_branch_kitchen_request_options",
                     "get_branch_kitchen_requests", "update_branch_kitchen_request_status"):
            self.assertIs(getattr(api, name), getattr(branch_requests, name))
        self.assertTrue(callable(api._apply_referral_for_mobile))
        self.assertTrue(callable(api._claim_first_order_referral_discount))
