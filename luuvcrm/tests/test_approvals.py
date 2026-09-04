"""Asking a manager, and what happens when they answer.

An approval is the only brake on a bill being changed after the fact, so the
state machine has to be exact: a request is answered once, the action fires only
on approval, and — for a purchase bill — the person who raised it cannot be the
one who waves it through.

Approving a purchase bill rewrites the buying price book, which is why that path
gets the most attention here: a wrong decision does not just affect one bill, it
changes what every future bill is measured against.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.tests import fixtures as fx


class ApprovalTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        api._install_pos_approval()
        self._approvals = []
        self.addCleanup(self._drop_approvals)

    def _drop_approvals(self):
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in self._approvals:
                if frappe.db.exists("POS Approval", name):
                    frappe.delete_doc("POS Approval", name, force=True, ignore_permissions=True)
        finally:
            frappe.set_user(user)

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def user_with(self, role, tag):
        email = f"lf-test-appr-{tag}@example.com"
        if not frappe.db.exists("User", email):
            here = frappe.session.user
            frappe.set_user("Administrator")
            frappe.get_doc({"doctype": "User", "email": email, "first_name": tag.title(),
                            "send_welcome_email": 0,
                            "roles": [{"role": role}]}).insert(ignore_permissions=True)
            frappe.set_user(here)
        return email

    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def request(self, **kwargs):
        kwargs.setdefault("approval_type", "discount")
        kwargs.setdefault("title", "Discount on table 4")
        name = self.call(api.create_approval, **kwargs)["name"]
        self._approvals.append(name)
        return name


class TestRaisingARequest(ApprovalTestCase):
    def test_a_new_request_starts_pending_and_remembers_who_asked(self):
        name = self.request()
        doc = frappe.get_doc("POS Approval", name)
        self.assertEqual(doc.status, "pending")
        self.assertEqual(doc.requested_by, frappe.session.user)
        self.assertTrue(doc.requested_at)

    def test_a_cashier_can_raise_one_without_admin_rights(self):
        """create_approval creates the POS Approval doctype on first use. While
        that work sat behind a System Manager guard, the first cashier to ask
        for a discount on a fresh site got a PermissionError instead."""
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        name = self.request()
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "pending")

    def test_kitchen_staff_cannot_raise_one(self):
        self.as_user(self.user_with(roles.KITCHEN_STAFF, "kitchen"))
        with self.assertRaises(frappe.PermissionError):
            self.call(api.create_approval, approval_type="discount", title="x")


class TestAnsweringARequest(ApprovalTestCase):
    """A request and its answer come from two different people — see
    TestSelfApproval — so these raise as the cashier and answer as the manager."""

    def raise_and_switch(self, **kwargs):
        """Raise a request as the cashier, then become the manager who answers it."""
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        name = self.request(**kwargs)
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        return name

    def test_approving_records_the_decision_and_the_decider(self):
        name = self.raise_and_switch()
        self.call(api.resolve_approval, name=name, decision="approved")
        doc = frappe.get_doc("POS Approval", name)
        self.assertEqual(doc.status, "approved")
        self.assertEqual(doc.resolved_by, frappe.session.user)
        self.assertTrue(doc.resolved_at)

    def test_rejecting_records_the_decision_too(self):
        name = self.raise_and_switch()
        self.call(api.resolve_approval, name=name, decision="rejected")
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "rejected")

    def test_a_request_is_answered_once(self):
        """Without this the same discount could be applied twice."""
        name = self.raise_and_switch()
        self.call(api.resolve_approval, name=name, decision="approved")
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.resolve_approval, name=name, decision="rejected")
        self.assertIn("Already resolved", str(caught.exception))

    def test_only_approved_or_rejected_are_decisions(self):
        name = self.raise_and_switch()
        for decision in ("maybe", "", "pending", None):
            with self.subTest(decision=decision):
                with self.assertRaises(frappe.ValidationError):
                    self.call(api.resolve_approval, name=name, decision=decision)
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "pending")

    def test_a_manager_comment_is_kept_with_the_decision(self):
        name = self.raise_and_switch()
        self.call(api.resolve_approval, name=name, decision="rejected",
                  comment="Too big — call me first")
        doc = frappe.get_doc("POS Approval", name)
        self.assertEqual(doc.get("manager_comment"), "Too big — call me first")


class TestSelfApproval(ApprovalTestCase):
    """Separation of duties: nobody answers their own request, whatever it is."""

    def test_the_person_who_entered_a_purchase_bill_cannot_approve_it(self):
        cashier = self.user_with(roles.CASHIER, "buyer")
        self.as_user(cashier)
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        with self.assertRaises(frappe.PermissionError) as caught:
            self.call(api.resolve_approval, name=name, decision="approved")
        self.assertIn("someone else has to answer", str(caught.exception))

    def test_nor_can_they_reject_it(self):
        cashier = self.user_with(roles.CASHIER, "buyer")
        self.as_user(cashier)
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_approval, name=name, decision="rejected")

    def test_no_role_is_exempt_from_that_rule(self):
        """A manager who enters a bill still needs a second pair of eyes."""
        manager = self.user_with(roles.RESTAURANT_MANAGER, "manager")
        self.as_user(manager)
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_approval, name=name, decision="approved")

    def test_somebody_else_may_approve_it(self):
        buyer = self.user_with(roles.CASHIER, "buyer")
        self.as_user(buyer)
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        self.call(api.resolve_approval, name=name, decision="approved")
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "approved")

    def test_a_cashier_cannot_approve_their_own_discount(self):
        """The gap this file first documented, now closed.

        _block_self_approval used to fire for purchase bills only, and
        resolve_approval's gate is a POS role rather than a manager one — so the
        cashier who asked for a discount could grant it themselves, which is the
        one thing the approval flow exists to prevent.
        """
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        name = self.request(approval_type="discount", title="20% off my own table")
        with self.assertRaises(frappe.PermissionError) as caught:
            self.call(api.resolve_approval, name=name, decision="approved")
        self.assertIn("someone else has to answer", str(caught.exception))
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "pending")

    def test_a_cashier_may_still_answer_a_colleague_s_request(self):
        """Two people on a shift can cover each other; the rule is about the same
        pair of hands, not about needing a manager on the premises."""
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        name = self.request(approval_type="discount", title="Discount on table 6")
        frappe.set_user(self.user_with(roles.CASHIER, "cashier2"))
        self.call(api.resolve_approval, name=name, decision="approved")
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "approved")

    def test_a_void_request_is_covered_too(self):
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        name = self.request(approval_type="void", title="Void table 2")
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_approval, name=name, decision="approved")


class TestPurchaseBillDecision(FrappeTestCase):
    """Approving a purchase bill rewrites the buying price book, so it decides
    what every later bill is compared against."""

    def setUp(self):
        super().setUp()
        api._install_purchase_bill()
        self.tomato = fx.item(self, "APPR-TOMATO")
        self._bills = []
        self.addCleanup(self._drop_bills)

    def _drop_bills(self):
        for name in self._bills:
            if frappe.db.exists("Purchase Bill", name):
                frappe.delete_doc("Purchase Bill", name, force=True, ignore_permissions=True)

    def bill(self, item, price, qty=1):
        doc = frappe.get_doc({
            "doctype": "Purchase Bill", "vendor": "_LFT-Vendor", "status": "pending",
            "total": price * qty, "requested_by": frappe.session.user,
            "items": [{"item": item, "qty": qty, "price": price}],
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        self._bills.append(doc.name)
        return doc

    def buying_price(self, item):
        return frappe.db.get_value("Item Price",
                                   {"item_code": item, "price_list": api._buying_price_list()},
                                   "price_list_rate")

    def test_approving_writes_the_buying_price(self):
        doc = self.bill(self.tomato, 320)
        api._decide_purchase_bill(doc.name, "approved")
        self.assertEqual(float(self.buying_price(self.tomato)), 320)

    def test_approving_again_moves_the_price(self):
        first = self.bill(self.tomato, 320)
        api._decide_purchase_bill(first.name, "approved")
        second = self.bill(self.tomato, 410)
        api._decide_purchase_bill(second.name, "approved")
        self.assertEqual(float(self.buying_price(self.tomato)), 410,
                         "a rising ingredient price must show up in the price book")

    def test_rejecting_leaves_the_price_book_alone(self):
        doc = self.bill(self.tomato, 999)
        api._decide_purchase_bill(doc.name, "rejected")
        self.assertIsNone(self.buying_price(self.tomato),
                          "a rejected bill must not set a price")

    def test_deciding_twice_changes_nothing_the_second_time(self):
        """Idempotent by status: only a pending bill is acted on, so a retried
        request cannot double-apply."""
        doc = self.bill(self.tomato, 320)
        self.assertEqual(api._decide_purchase_bill(doc.name, "approved"), 1)
        self.assertEqual(api._decide_purchase_bill(doc.name, "approved"), 0)

    def test_an_unchanged_price_is_not_counted_as_an_update(self):
        first = self.bill(self.tomato, 320)
        api._decide_purchase_bill(first.name, "approved")
        same = self.bill(self.tomato, 320)
        self.assertEqual(api._decide_purchase_bill(same.name, "approved"), 0,
                         "re-buying at the same price is not a price change")

    def test_a_line_without_a_price_is_skipped(self):
        doc = self.bill(self.tomato, 0)
        api._decide_purchase_bill(doc.name, "approved")
        self.assertIsNone(self.buying_price(self.tomato))
