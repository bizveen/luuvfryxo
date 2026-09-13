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

    def buyer(self):
        """Somebody who can both raise a purchase approval and decide one.

        A cashier alone no longer reaches the second half: purchase bills are
        decided by `roles.PURCHASE_ROLES`, so a plain cashier is stopped by that
        gate before the self-approval rule is ever asked. To test the rule, the
        buyer has to be somebody the gate lets through — a cashier who is also a
        Purchaser. Its own tag, so the extra role never leaks onto the "buyer"
        account the tests below use as a plain cashier.
        """
        email = self.user_with(roles.CASHIER, "buyer-purchaser")
        if roles.PURCHASER not in frappe.get_roles(email):
            frappe.get_doc("User", email).add_roles(roles.PURCHASER)
        return email

    def test_the_person_who_entered_a_purchase_bill_cannot_approve_it(self):
        self.as_user(self.buyer())
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        with self.assertRaises(frappe.PermissionError) as caught:
            self.call(api.resolve_approval, name=name, decision="approved")
        self.assertIn("someone else has to answer", str(caught.exception))

    def test_nor_can_they_reject_it(self):
        self.as_user(self.buyer())
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

    def test_a_plain_cashier_cannot_decide_a_purchase_bill_at_all(self):
        """Not their own and not anybody else's: purchasing is a role, and this
        endpoint decides discounts and voids for the till but purchase bills only
        for somebody who holds it."""
        buyer = self.user_with(roles.RESTAURANT_MANAGER, "manager")
        self.as_user(buyer)
        name = self.request(approval_type="bill", action="purchase_bill",
                            action_payload=json.dumps({"bill": "_LFT-NO-SUCH-BILL"}))
        frappe.set_user(self.user_with(roles.CASHIER, "not-a-buyer"))
        with self.assertRaises(frappe.PermissionError) as caught:
            self.call(api.resolve_approval, name=name, decision="approved")
        self.assertIn("purchasing", str(caught.exception))
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "pending")

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


class TestTheGuardsCannotBeWalkedAround(ApprovalTestCase):
    """`resolve_approval` refuses to let somebody answer their own request.

    But the guard is in *our* endpoint, and Frappe exposes every doctype over
    its own generic REST API, where DocPerms are the only thing standing in the
    way. POS Approval was created granting Cashier read/write/create/delete, so
    the question is whether a cashier can simply write `status` on the row and
    skip the whole flow — the same bug as "a cashier could approve their own
    discount", reached through a different door.
    """

    def setUp(self):
        super().setUp()
        self.cashier = self.user_with(roles.CASHIER, "walkaround")

    def _own_request(self):
        self.as_user(self.cashier)
        result = self.call(api.create_approval, approval_type="discount",
                           title="_LFT walkaround")
        name = result["name"] if isinstance(result, dict) else result
        self._approvals.append(name)
        return name

    def test_the_endpoint_refuses_a_cashier_answering_their_own_request(self):
        name = self._own_request()
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_approval, name=name, decision="approved")

    def test_and_so_does_writing_the_row_directly(self):
        """The one that matters. If this passes by writing the field, every
        guard in approvals.py is advisory."""
        name = self._own_request()
        self.as_user(self.cashier)
        with self.assertRaises(frappe.PermissionError):
            doc = frappe.get_doc("POS Approval", name)
            doc.status = "approved"
            doc.save()
        frappe.set_user("Administrator")
        self.assertEqual(frappe.db.get_value("POS Approval", name, "status"), "pending")

    def test_nor_can_a_cashier_delete_the_record_of_it(self):
        """An approval trail somebody can erase is not a trail."""
        name = self._own_request()
        self.as_user(self.cashier)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc("POS Approval", name)
        frappe.set_user("Administrator")
        self.assertTrue(frappe.db.exists("POS Approval", name))


class TestRepeatedTapsCollapse(ApprovalTestCase):
    """Brought over from `main`, where it was found on the floor. The POS leaves
    a removed or edited item on screen until the manager approves, so nothing
    visibly changes after a tap and the cashier taps again — one order once
    carried ten identical "remove" cards, and a manager had to clear all ten.

    The key is the *whole* action, so only true repeats collapse: a different
    item or quantity makes a different payload and still gets its own card.
    """

    def test_an_identical_pending_request_is_returned_not_remade(self):
        first = self.request(action="void", action_payload='{"item":"TAP1"}')
        again = self.call(api.create_approval, approval_type="discount",
                          title="Discount on table 4",
                          action="void", action_payload='{"item":"TAP1"}')
        self.assertEqual(again["name"], first)
        self.assertTrue(again["duplicate"])
        self.assertEqual(frappe.db.count("POS Approval",
                                         {"action_payload": '{"item":"TAP1"}',
                                          "status": "pending"}), 1)

    def test_a_different_payload_still_raises_its_own_card(self):
        first = self.request(action="void", action_payload='{"item":"TAP2-A"}')
        second = self.request(action="void", action_payload='{"item":"TAP2-B"}')
        self.assertNotEqual(first, second)

    def test_a_resolved_request_does_not_swallow_the_next_one(self):
        """Only a *pending* twin collapses. The same change asked for again
        after a manager has answered is a new question."""
        first = self.request(action="void", action_payload='{"item":"TAP3"}')
        frappe.db.set_value("POS Approval", first, "status", "approved")
        second = self.request(action="void", action_payload='{"item":"TAP3"}')
        self.assertNotEqual(second, first)

    def test_two_people_asking_the_same_thing_are_two_requests(self):
        """`requested_by` is in the dedupe key, which is a deliberate difference
        from where this came from. Without it the second cashier is handed the
        first one's card and can then resolve it, because the record says
        somebody else raised it — the exact hole block_self_approval() closes."""
        first = self.request(action="void", action_payload='{"item":"TAP5"}')
        self.as_user(self.user_with(roles.CASHIER, "twin"))
        second = self.request(action="void", action_payload='{"item":"TAP5"}')
        self.assertNotEqual(first, second)

    def test_the_trail_gets_one_row_for_one_request(self):
        """The collapsed tap writes nothing: it is the same request, and the row
        for it was written when it was first raised."""
        from luuvcrm import activity

        name = self.request(action="void", action_payload='{"item":"TAP4"}')
        self.call(api.create_approval, approval_type="discount", title="x",
                  action="void", action_payload='{"item":"TAP4"}')
        rows = frappe.db.sql(
            "select count(*) from `tab{0}` where action = 'approval.create' "
            "and ref_name = %s".format(activity.DOCTYPE), (name,))[0][0]
        self.assertEqual(rows, 1)


class TestThePurchaseCardShowsThePriceMove(FrappeTestCase):
    """"Was Rs 300" on a purchase approval card is the one number that says a
    supplier put their prices up, and the manager is looking at the card at the
    moment they can still refuse it.

    The stored card lines carry only qty/name/total, so the per-line price is
    overlaid from the linked bill — matched by row order, because both are built
    in the same loop in create_purchase_bill. This was lost once already in a
    revert (`851f74a`), which is why it has a test now.
    """

    def setUp(self):
        super().setUp()
        api._install_purchase_bill()
        api._install_pos_approval()
        self.item = fx.item(self, "APPR-DELTA-ITEM")
        self._bills = []
        self.addCleanup(self._drop)

    def _drop(self):
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in self._bills:
                if frappe.db.exists("Purchase Bill", name):
                    frappe.delete_doc("Purchase Bill", name, force=True, ignore_permissions=True)
            for price in frappe.get_all("Item Price", pluck="name",
                                        filters={"item_code": self.item}):
                frappe.delete_doc("Item Price", price, force=True, ignore_permissions=True)
        finally:
            frappe.set_user(user)

    def enter(self, price):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(
            vendor="_LFT-Vendor", lines=json.dumps([{"item": self.item, "qty": 2, "price": price}]))
        try:
            name = api.create_purchase_bill()["name"]
        finally:
            frappe.local.form_dict = previous
        self._bills.append(name)
        return name

    def card_for(self, bill):
        rows = api._pending_and_recent_approvals(action="purchase_bill")
        for card in rows:
            payload = json.loads(card.get("action_payload") or "{}") \
                if isinstance(card, dict) and card.get("action_payload") else {}
            if payload.get("bill") == bill:
                return card
        return None

    def test_the_card_line_carries_the_price_and_the_previous_one(self):
        first = self.enter(300)
        api._decide_purchase_bill(first, "approved")
        second = self.enter(360)
        cards = api._format_approvals(api._pending_and_recent_approvals(action="purchase_bill"))
        card = next((c for c in cards
                     if json.loads(frappe.db.get_value("POS Approval", c["name"],
                                                       "action_payload") or "{}").get("bill") == second),
                    None)
        self.assertIsNotNone(card, "the second bill should have a pending card")
        line = card["lines"][0]
        self.assertEqual(float(line["price"]), 360)
        self.assertEqual(float(line["old_price"]), 300,
                         "the delta is the previous *unit* price, not the line total")

    def test_a_first_purchase_shows_no_previous_price(self):
        bill = self.enter(300)
        cards = api._format_approvals(api._pending_and_recent_approvals(action="purchase_bill"))
        card = next((c for c in cards
                     if json.loads(frappe.db.get_value("POS Approval", c["name"],
                                                       "action_payload") or "{}").get("bill") == bill),
                    None)
        self.assertIsNotNone(card)
        self.assertIn(float(card["lines"][0].get("old_price") or 0), (0.0,))

    def test_the_unit_comes_across_too(self):
        bill = self.enter(300)
        cards = api._format_approvals(api._pending_and_recent_approvals(action="purchase_bill"))
        card = next((c for c in cards
                     if json.loads(frappe.db.get_value("POS Approval", c["name"],
                                                       "action_payload") or "{}").get("bill") == bill),
                    None)
        self.assertTrue(card["lines"][0].get("unit"))


class TestBillChangesLand(ApprovalTestCase):
    """Approving a bill change has to change the bill — or say why it could not.

    For two weeks (27 Aug – 9 Sep 2026) the execution failed 89 times and every
    one of those cards still read "approved": the manager saw a green tick, the
    till heard "approved", and the item stayed. Three shapes, each pinned here:
    a bill emptied of every item (an order cannot be saved with none), a card
    approved after the bill was voided or paid, and an older card approved
    after a newer one for the same bill, which put the older item list back.
    """

    def setUp(self):
        super().setUp()
        self.burger = fx.item(self, "APPR-BURGER")
        fx.item_price(self, self.burger, 1000)
        self.cola = fx.item(self, "APPR-COLA")
        fx.item_price(self, self.cola, 250)
        fx.service_charge(self, 10)
        self._orders = []
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in self._orders:
                if frappe.db.exists("POS Order", name):
                    frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
        finally:
            frappe.set_user(user)

    def order(self, items):
        result = self.call(api.send_to_kitchen, items=json.dumps(items), order_type="Dine In")
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        return name

    def items_of(self, order):
        return sorted(i.item for i in frappe.get_doc("POS Order", order).items)

    def cashier(self):
        self.as_user(self.user_with(roles.CASHIER, "cashier"))

    def manager(self):
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))

    def edit(self, order, items):
        return self.request(approval_type="edit", title="Remove item approval",
                            pos_order=order, action="update_items",
                            action_payload=json.dumps({"items": items}))

    def approve(self, name):
        return self.call(api.resolve_approval, name=name, decision="approved")

    def test_removing_every_item_voids_the_bill(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.cashier()
        card = self.edit(order, [])
        self.manager()
        result = self.approve(card)
        self.assertEqual(result["executed"], "voided")
        self.assertIsNone(result["note"])
        self.assertEqual(frappe.db.get_value("POS Order", order, "docstatus"), 2)
        self.assertEqual(frappe.db.get_value("POS Approval", card, "status"), "approved")

    def test_a_newer_edit_replaces_the_older_one(self):
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.cashier()
        older = self.edit(order, [{"item": self.burger, "qty": 1}])
        newer = self.edit(order, [{"item": self.cola, "qty": 1}])
        self.assertEqual(frappe.db.get_value("POS Approval", older, "status"), "superseded")
        self.manager()
        self.assertEqual(self.approve(newer)["executed"], "items_updated")
        self.assertEqual(self.items_of(order), [self.cola])

    def test_a_stale_edit_is_not_applied_over_a_newer_one(self):
        """Belt and braces for the case above: a card that somehow stays
        pending beside a newer one must still not put the old list back."""
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.cashier()
        older = self.edit(order, [{"item": self.burger, "qty": 1}])
        self.edit(order, [{"item": self.cola, "qty": 1}])
        frappe.db.set_value("POS Approval", older, "status", "pending")
        self.manager()
        result = self.approve(older)
        self.assertIsNone(result["executed"])
        self.assertIn("newer", result["note"])
        self.assertEqual(frappe.db.get_value("POS Approval", older, "status"), "rejected")
        self.assertEqual(self.items_of(order), [self.burger, self.cola])

    def test_a_change_to_a_paid_bill_is_closed_with_the_reason(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.cashier()
        card = self.request(approval_type="void", title="Void", pos_order=order, action="void")
        frappe.db.set_value("POS Order", order, "pos_invoice", "ACC-PSINV-TEST-1")
        self.manager()
        result = self.approve(card)
        self.assertIsNone(result["executed"])
        self.assertEqual(result["status"], "rejected")
        self.assertIn("paid", result["note"])
        doc = frappe.get_doc("POS Approval", card)
        self.assertEqual(doc.status, "rejected")
        self.assertIn("paid", doc.manager_comment)
        self.assertEqual(frappe.db.get_value("POS Order", order, "docstatus"), 0)

    def test_a_change_to_a_voided_bill_is_closed_with_the_reason(self):
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.cashier()
        card = self.edit(order, [{"item": self.burger, "qty": 1}])
        frappe.db.set_value("POS Order", order, "docstatus", 2)
        self.manager()
        result = self.approve(card)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("voided", result["note"])
        self.assertEqual(self.items_of(order), [self.burger, self.cola])

    def test_a_failure_rolls_the_decision_back(self):
        """An error while applying the change is not swallowed any more: it
        reaches the manager and the card stays pending, so nobody is told
        "approved" about a change that did not happen."""
        from unittest.mock import patch

        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.cashier()
        card = self.edit(order, [{"item": self.burger, "qty": 1}])
        self.manager()
        # A request that throws is rolled back whole by Frappe; inside the test
        # transaction that is a savepoint.
        frappe.db.savepoint("approval_failed")
        with patch("luuvcrm.api.approvals._apply_order_items",
                   side_effect=frappe.ValidationError("kitchen is on fire")):
            with self.assertRaises(frappe.ValidationError):
                self.approve(card)
        frappe.db.rollback(save_point="approval_failed")
        self.assertEqual(frappe.db.get_value("POS Approval", card, "status"), "pending")
        self.assertEqual(self.items_of(order), [self.burger, self.cola])
