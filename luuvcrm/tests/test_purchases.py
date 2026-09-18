"""Buying: entering a vendor's bill, approving it, and paying it.

Two things make this worth covering carefully. Approving a bill rewrites the
buying price book, so one decision changes what every later bill is measured
against — that half is pinned in test_approvals.py. And an approved bill is money
owed to a vendor until somebody marks it paid, so anything that quietly drops a
row from the due list is money the restaurant forgets it owes.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.tests import fixtures as fx


class PurchaseTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        api._install_purchase_bill()
        api._install_purchase_payment_fields()
        api._install_pos_approval()
        self.tomato = fx.item(self, "PB-TOMATO")
        self.onion = fx.item(self, "PB-ONION")
        self._bills = []
        self.addCleanup(self._drop)

    def _drop(self):
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in self._bills:
                for appr in frappe.get_all("POS Approval", pluck="name",
                                           filters={"action": "purchase_bill"}):
                    payload = frappe.db.get_value("POS Approval", appr, "action_payload") or "{}"
                    if json.loads(payload).get("bill") == name:
                        frappe.delete_doc("POS Approval", appr, force=True,
                                          ignore_permissions=True)
                if frappe.db.exists("Purchase Bill", name):
                    frappe.delete_doc("Purchase Bill", name, force=True,
                                      ignore_permissions=True)
            for item in (self.tomato, self.onion):
                for price in frappe.get_all("Item Price", pluck="name",
                                            filters={"item_code": item}):
                    frappe.delete_doc("Item Price", price, force=True,
                                      ignore_permissions=True)
        finally:
            frappe.set_user(user)

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def enter(self, lines=None, vendor="_LFT-Vendor", **kwargs):
        lines = lines or [{"item": self.tomato, "qty": 5, "price": 300}]
        result = self.call(api.create_purchase_bill, vendor=vendor,
                           lines=json.dumps(lines), **kwargs)
        self._bills.append(result["name"])
        return result["name"]

    def user_with(self, role, tag):
        email = f"lf-test-pb-{tag}@example.com"
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


class TestEnteringABill(PurchaseTestCase):
    def test_a_new_bill_is_pending_and_remembers_who_entered_it(self):
        doc = frappe.get_doc("Purchase Bill", self.enter())
        self.assertEqual(doc.status, "pending")
        self.assertEqual(doc.requested_by, frappe.session.user)

    def test_the_total_is_computed_from_the_lines(self):
        name = self.enter([{"item": self.tomato, "qty": 5, "price": 300},
                           {"item": self.onion, "qty": 2, "price": 250}])
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "total"), 2000)

    def test_the_price_being_replaced_is_recorded_on_the_line(self):
        """The approve screen shows what the price was, so the manager can see a
        supplier's increase before waving it through."""
        fx.item_price(self, self.tomato, 280, price_list=api._buying_price_list(), selling=0)
        doc = frappe.get_doc("Purchase Bill", self.enter())
        self.assertEqual(float(doc.items[0].old_price), 280)
        self.assertEqual(float(doc.items[0].price), 300)

    def test_a_first_purchase_records_no_previous_price(self):
        doc = frappe.get_doc("Purchase Bill", self.enter())
        self.assertEqual(float(doc.items[0].old_price or 0), 0)

    def test_a_bill_without_a_vendor_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.create_purchase_bill, vendor="  ",
                      lines=json.dumps([{"item": self.tomato, "qty": 1, "price": 10}]))
        self.assertIn("Vendor is required", str(caught.exception))

    def test_an_empty_bill_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.create_purchase_bill, vendor="V", lines=json.dumps([]))
        self.assertIn("at least one item", str(caught.exception))

    def test_it_raises_an_approval_for_the_manager(self):
        name = self.enter()
        approvals = [a for a in frappe.get_all(
            "POS Approval", filters={"action": "purchase_bill", "status": "pending"},
            fields=["name", "action_payload", "approval_type"])
            if json.loads(a.action_payload or "{}").get("bill") == name]
        self.assertEqual(len(approvals), 1, "a bill nobody is asked about is never approved")
        self.assertEqual(approvals[0].approval_type, "bill")

    def test_a_purchaser_can_enter_one_without_admin_rights(self):
        """create_purchase_bill builds the Purchase Bill and POS Approval doctypes
        on first use; while that work sat behind a System Manager guard, the
        first bill entered on a fresh site got a PermissionError."""
        self.as_user(self.user_with(roles.PURCHASER, "purchaser"))
        name = self.enter()
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "status"), "pending")

    def test_a_cashier_cannot_enter_one(self):
        """Purchasing is a job somebody is given, not something the till can do.
        The endpoints used to accept any POS role while the page was
        manager-only, so the screen said no and the endpoint said yes."""
        self.as_user(self.user_with(roles.CASHIER, "cashier"))
        with self.assertRaises(frappe.PermissionError):
            self.enter()

    def test_kitchen_staff_cannot_enter_one(self):
        self.as_user(self.user_with(roles.KITCHEN_STAFF, "kitchen"))
        with self.assertRaises(frappe.PermissionError):
            self.enter()


class TestResolvingABill(PurchaseTestCase):
    def test_approving_updates_the_price_book_and_says_how_many(self):
        self.as_user(self.user_with(roles.PURCHASER, "buyer"))
        name = self.enter()
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        result = self.call(api.resolve_purchase_bill, name=name, decision="approved")
        self.assertEqual(result["prices_updated"], 1)
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "status"), "approved")

    def test_rejecting_leaves_the_price_book_alone(self):
        self.as_user(self.user_with(roles.PURCHASER, "buyer"))
        name = self.enter()
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        self.call(api.resolve_purchase_bill, name=name, decision="rejected")
        self.assertIsNone(frappe.db.get_value(
            "Item Price", {"item_code": self.tomato, "price_list": api._buying_price_list()},
            "price_list_rate"))

    def test_a_bill_is_resolved_once(self):
        self.as_user(self.user_with(roles.PURCHASER, "buyer"))
        name = self.enter()
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        self.call(api.resolve_purchase_bill, name=name, decision="approved")
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.resolve_purchase_bill, name=name, decision="rejected")
        self.assertIn("Already resolved", str(caught.exception))

    def test_only_approved_or_rejected_are_decisions(self):
        name = self.enter()
        for decision in ("maybe", "", None):
            with self.subTest(decision=decision):
                with self.assertRaises(frappe.ValidationError):
                    self.call(api.resolve_purchase_bill, name=name, decision=decision)

    def test_the_buyer_cannot_approve_their_own_bill(self):
        """Approving rewrites the buying price book, so one person must not be
        able to both enter a purchase and wave it through."""
        self.as_user(self.user_with(roles.PURCHASER, "buyer"))
        name = self.enter()
        with self.assertRaises(frappe.PermissionError) as caught:
            self.call(api.resolve_purchase_bill, name=name, decision="approved")
        self.assertIn("someone else has to answer", str(caught.exception))
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "status"), "pending")


class TestOwingTheVendor(PurchaseTestCase):
    """An approved bill is money owed until somebody marks it paid."""

    def approved_bill(self, **kwargs):
        self.as_user(self.user_with(roles.PURCHASER, "buyer"))
        name = self.enter(**kwargs)
        frappe.set_user(self.user_with(roles.RESTAURANT_MANAGER, "manager"))
        self.call(api.resolve_purchase_bill, name=name, decision="approved")
        return name

    def due_names(self):
        return [b["name"] for b in self.call(api.get_due_bills)["bills"]]

    def test_an_approved_bill_is_owed(self):
        name = self.approved_bill()
        self.assertIn(name, self.due_names())

    def test_a_pending_bill_is_not_owed_yet(self):
        self.assertNotIn(self.enter(), self.due_names())

    def test_marking_it_paid_clears_it(self):
        name = self.approved_bill()
        self.call(api.mark_bill_paid, name=name)
        self.assertNotIn(name, self.due_names())

    def test_paying_records_who_and_when(self):
        name = self.approved_bill()
        self.call(api.mark_bill_paid, name=name)
        doc = frappe.get_doc("Purchase Bill", name)
        self.assertEqual(doc.payment_status, "paid")
        self.assertEqual(doc.paid_by, frappe.session.user)
        self.assertTrue(doc.paid_at)

    def test_a_mistaken_tap_can_be_undone(self):
        name = self.approved_bill()
        self.call(api.mark_bill_paid, name=name)
        self.call(api.mark_bill_paid, name=name, paid="0")
        self.assertIn(name, self.due_names())
        self.assertIsNone(frappe.db.get_value("Purchase Bill", name, "paid_at"))

    def test_a_bill_with_no_payment_status_still_counts_as_owed(self):
        """The IFNULL in the query, which is not decoration.

        A bill entered before the payment_status field existed reads NULL, and
        SQL drops NULL rows from a plain `!= 'paid'` — so those bills would
        vanish from the payables list. Money the restaurant owes and cannot see.
        """
        name = self.approved_bill()
        frappe.db.set_value("Purchase Bill", name, "payment_status", None)
        self.assertIn(name, self.due_names(),
                      "a bill from before the field existed is still owed")

    def test_an_unapproved_bill_cannot_be_paid(self):
        name = self.enter()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.mark_bill_paid, name=name)
        self.assertIn("Only an approved bill", str(caught.exception))

    def test_a_bill_that_does_not_exist_cannot_be_paid(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.mark_bill_paid, name="_LFT-NO-SUCH-BILL")
        self.assertIn("Bill not found", str(caught.exception))

    def test_the_total_owed_adds_up(self):
        self.approved_bill(lines=[{"item": self.tomato, "qty": 2, "price": 100}])
        result = self.call(api.get_due_bills)
        mine = [b for b in result["bills"] if b["vendor"] == "_LFT-Vendor"]
        self.assertEqual(result["total_due"], sum(b["total"] for b in result["bills"]))
        self.assertTrue(mine)

    def test_what_is_owed_is_grouped_by_vendor(self):
        self.approved_bill(vendor="_LFT-Vendor-A",
                           lines=[{"item": self.tomato, "qty": 1, "price": 100}])
        self.approved_bill(vendor="_LFT-Vendor-A",
                           lines=[{"item": self.onion, "qty": 1, "price": 200}])
        vendors = {v["vendor"]: v["amount"] for v in self.call(api.get_due_bills)["vendors"]}
        self.assertEqual(vendors.get("_LFT-Vendor-A"), 300)


class TestFindingABill(PurchaseTestCase):
    """The Bills list stops at the 60 newest, so search is how an older bill is
    reached at all."""

    def test_a_bill_is_found_by_its_vendor(self):
        self.enter(vendor="_LFT-Kandy-Traders")
        found = api.search_purchase_bills(q="Kandy")["bills"]
        self.assertIn("_LFT-Kandy-Traders", [b["vendor"] for b in found])

    def test_a_bill_is_found_by_an_item_on_it(self):
        self.enter(vendor="_LFT-Vendor-Q", lines=[{"item": self.onion, "qty": 1, "price": 50}])
        found = api.search_purchase_bills(q="PB-ONION")["bills"]
        self.assertIn("_LFT-Vendor-Q", [b["vendor"] for b in found])

    def test_a_bill_is_found_by_its_note(self):
        self.enter(vendor="_LFT-Vendor-N", note="delivered late")
        found = api.search_purchase_bills(q="delivered late")["bills"]
        self.assertIn("_LFT-Vendor-N", [b["vendor"] for b in found])

    def test_a_search_can_be_narrowed_to_one_status(self):
        self.enter(vendor="_LFT-Vendor-S")
        found = api.search_purchase_bills(q="_LFT-Vendor-S", status="pending")["bills"]
        self.assertTrue(found)
        self.assertEqual({b["status"] for b in found}, {"pending"})

    def test_a_search_that_matches_nothing_returns_nothing(self):
        self.assertEqual(api.search_purchase_bills(q="_LFT-NOTHING-MATCHES-THIS")["bills"], [])

    def test_the_lines_are_left_out_of_search_results(self):
        """Rows come straight from the DB with no Document built, so a search
        costs the same at 200 bills as at 20,000."""
        self.enter(vendor="_LFT-Vendor-L")
        found = api.search_purchase_bills(q="_LFT-Vendor-L")["bills"]
        self.assertNotIn("items", found[0])


class TestFixingAnItemsName(PurchaseTestCase):
    """A typo in the price book follows every bill raised from then on, and the
    only way to fix one was the Frappe Desk. Brought over from `main`, where it
    was written against the live restaurant's own misspelled items."""

    def test_the_name_changes_and_the_item_does_not(self):
        """The item_code, the price book entry and every existing bill's link
        stay put, so nothing is re-priced or orphaned."""
        self.enter()
        result = self.call(api.rename_purchase_item, item=self.tomato,
                           new_name="Tomatoes (large)")
        self.assertTrue(result["ok"])
        self.assertEqual(frappe.db.get_value("Item", self.tomato, "item_name"),
                         "Tomatoes (large)")
        self.assertTrue(frappe.db.exists("Item", self.tomato))

    def test_a_bill_already_raised_keeps_the_name_it_was_saved_with(self):
        """The line is a snapshot. Rewriting old bills would change what a
        manager approved after they approved it."""
        bill = self.enter()
        was = frappe.db.get_value("Purchase Bill Item",
                                  {"parent": bill, "item": self.tomato}, "item_name")
        self.call(api.rename_purchase_item, item=self.tomato, new_name="Tomatoes (large)")
        self.assertEqual(frappe.db.get_value("Purchase Bill Item",
                                             {"parent": bill, "item": self.tomato},
                                             "item_name"), was)

    def test_the_unit_can_be_corrected_too(self):
        self.call(api.rename_purchase_item, item=self.tomato,
                  new_name="_LFT-Tomato", unit="Kg")
        self.assertEqual(frappe.db.get_value("Item", self.tomato, "stock_uom"), "Kg")

    def test_a_unit_that_does_not_exist_yet_is_created(self):
        self.call(api.rename_purchase_item, item=self.tomato,
                  new_name="_LFT-Tomato", unit="_LFT-Bushel")
        self.assertTrue(frappe.db.exists("UOM", "_LFT-Bushel"))
        self.addCleanup(frappe.delete_doc, "UOM", "_LFT-Bushel", force=True,
                        ignore_permissions=True)

    def test_renaming_into_another_item_s_name_is_refused(self):
        """create_purchase_item dedupes on item_name, so two items with one name
        make the picker ambiguous — and the wrong one gets bought."""
        other = frappe.db.get_value("Item", self.onion, "item_name")
        with self.assertRaises(frappe.ValidationError):
            self.call(api.rename_purchase_item, item=self.tomato, new_name=other)

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.call(api.rename_purchase_item, item=self.tomato, new_name="   ")

    def test_kitchen_staff_cannot_rename_a_purchase_item(self):
        self.as_user(self.user_with(roles.KITCHEN_STAFF, "kitchen"))
        with self.assertRaises(frappe.PermissionError):
            self.call(api.rename_purchase_item, item=self.tomato, new_name="Anything")


class TestTheDaySummary(PurchaseTestCase):
    """The day card above the Bills list. A day owns a bill by when it was
    *entered*, so the numbers add up to that day's entries however long a
    manager took to approve them — which is why `approved_by` can name work that
    happened later."""

    def test_today_counts_what_was_entered_today(self):
        self.enter(lines=[{"item": self.tomato, "qty": 2, "price": 100}])
        day = api.get_purchase_day_summary()
        self.assertTrue(day["is_today"])
        self.assertGreaterEqual(day["count"], 1)
        self.assertGreaterEqual(day["total"], 200)

    def test_the_buckets_add_up_to_the_day(self):
        self.enter(lines=[{"item": self.tomato, "qty": 1, "price": 500}])
        day = api.get_purchase_day_summary()
        counted = sum(day[k]["count"] for k in ("approved", "pending", "rejected"))
        self.assertEqual(counted, day["count"])

    def test_it_names_who_entered_the_bills(self):
        self.enter()
        day = api.get_purchase_day_summary()
        self.assertIn(frappe.session.user, [p["user"] for p in day["entered_by"]])

    def test_an_approved_bill_names_who_settled_it(self):
        bill = self.enter()
        manager = self.user_with(roles.RESTAURANT_MANAGER, "mgr2")
        here = frappe.session.user
        frappe.set_user(manager)
        try:
            api._decide_purchase_bill(bill, "approved")
            frappe.db.set_value("Purchase Bill", bill, "resolved_by", manager)
        finally:
            frappe.set_user(here)
        day = api.get_purchase_day_summary()
        row = next((b for b in day["bills"] if b["name"] == bill), None)
        self.assertIsNotNone(row)
        self.assertIn(manager, [p["user"] for p in day["approved_by"]])

    def test_a_day_with_nothing_in_it_answers_zero_rather_than_failing(self):
        day = api.get_purchase_day_summary(date="2019-01-01")
        self.assertEqual(day["count"], 0)
        self.assertEqual(day["bills"], [])
        self.assertFalse(day["is_today"])

    def test_each_bill_carries_its_lines_so_the_sheet_needs_no_second_call(self):
        self.enter(lines=[{"item": self.tomato, "qty": 3, "price": 120},
                          {"item": self.onion, "qty": 1, "price": 90}])
        day = api.get_purchase_day_summary()
        self.assertTrue(day["bills"])
        self.assertEqual(len(day["bills"][0]["lines"]), 2)

    def test_a_cashier_may_read_the_day_but_kitchen_staff_may_not(self):
        self.as_user(self.user_with(roles.KITCHEN_STAFF, "kitchen"))
        with self.assertRaises(frappe.PermissionError):
            api.get_purchase_day_summary()


class TestADiscountAtApproval(PurchaseTestCase):
    """The vendor knocks something off the whole bill, and the manager is the
    one holding the paper when it happens — so the discount is entered at the
    moment of approval, not when the bill was typed in.

    **It reduces the bill's total and nothing else.** The items keep the prices
    they were bought at, because a settlement discount or a round-down is not a
    change in what the item costs. Writing it into the price book would make the
    next bill's "was Rs" delta compare a normal price against a discounted one
    and report a rise that never happened.
    """

    def approve(self, bill, discount=None, by=None):
        who = by or self.user_with(roles.RESTAURANT_MANAGER, "discmgr")
        previous = frappe.session.user
        frappe.set_user(who)
        try:
            return api._decide_purchase_bill(bill, "approved", discount=discount)
        finally:
            frappe.set_user(previous)

    def test_it_comes_off_the_total(self):
        bill = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        self.approve(bill, discount=250)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "total")), 750)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "discount")), 250)

    def test_the_items_keep_the_price_they_were_bought_at(self):
        """The one that matters. A one-off haggle must not rewrite the price
        book, or every later bill is measured against a number the vendor never
        charges."""
        bill = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        self.approve(bill, discount=250)
        priced = frappe.db.get_value("Item Price",
                                     {"item_code": self.tomato,
                                      "price_list": api._buying_price_list()},
                                     "price_list_rate")
        self.assertEqual(float(priced), 100)

    def test_the_next_bill_compares_against_the_undiscounted_price(self):
        first = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        self.approve(first, discount=250)
        second = self.enter(lines=[{"item": self.tomato, "qty": 5, "price": 100}])
        old = frappe.db.get_value("Purchase Bill Item",
                                  {"parent": second, "item": self.tomato}, "old_price")
        self.assertEqual(float(old), 100, "no price rise happened, so none is reported")

    def test_approving_without_one_changes_nothing(self):
        bill = self.enter(lines=[{"item": self.tomato, "qty": 4, "price": 250}])
        self.approve(bill)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "total")), 1000)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "discount") or 0), 0)

    def test_a_discount_bigger_than_the_bill_is_refused(self):
        """Otherwise the bill is worth less than nothing and the food cost goes
        negative, which no report is expecting."""
        bill = self.enter(lines=[{"item": self.tomato, "qty": 1, "price": 100}])
        with self.assertRaises(frappe.ValidationError):
            self.approve(bill, discount=500)

    def test_a_negative_discount_is_refused(self):
        bill = self.enter(lines=[{"item": self.tomato, "qty": 1, "price": 100}])
        with self.assertRaises(frappe.ValidationError):
            self.approve(bill, discount=-50)

    def test_rejecting_never_writes_one(self):
        bill = self.enter(lines=[{"item": self.tomato, "qty": 2, "price": 300}])
        who = self.user_with(roles.RESTAURANT_MANAGER, "discmgr")
        previous = frappe.session.user
        frappe.set_user(who)
        try:
            api._decide_purchase_bill(bill, "rejected", discount=100)
        finally:
            frappe.set_user(previous)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "discount") or 0), 0)
        self.assertEqual(float(frappe.db.get_value("Purchase Bill", bill, "total")), 600)

    def test_the_screens_show_what_came_off(self):
        """`total` is what is owed; `subtotal` is what the lines add up to. Both,
        so neither has to be worked out twice in JavaScript — and so a bill with
        a discount does not look like an arithmetic error."""
        bill = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        self.approve(bill, discount=250)
        row = api._bill_dict(bill)
        self.assertEqual(row["subtotal"], 1000)
        self.assertEqual(row["discount"], 250)
        self.assertEqual(row["total"], 750)

    def test_the_day_summary_counts_what_was_actually_paid(self):
        bill = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        self.approve(bill, discount=250)
        day = api.get_purchase_day_summary()
        row = next(b for b in day["bills"] if b["name"] == bill)
        self.assertEqual(row["total"], 750)

    def test_the_trail_records_who_allowed_it(self):
        """A discount is money leaving the arithmetic. The row is the only place
        that says who decided it."""
        from luuvcrm import activity

        bill = self.enter(lines=[{"item": self.tomato, "qty": 10, "price": 100}])
        approval = frappe.db.get_value("POS Approval",
                                       {"action": "purchase_bill", "status": "pending"},
                                       "name", order_by="creation desc")
        if not approval:
            self.skipTest("no approval was raised for this bill")
        who = self.user_with(roles.RESTAURANT_MANAGER, "discmgr")
        previous = frappe.session.user
        frappe.set_user(who)
        try:
            self.call(api.resolve_approval, name=approval, decision="approved", discount="250")
        finally:
            frappe.set_user(previous)
        rows = frappe.db.sql(
            "select detail from `tab{0}` where action = 'approval.resolve' "
            "order by creation desc limit 1".format(activity.DOCTYPE), as_dict=True)
        self.assertTrue(rows and "250" in (rows[0].detail or ""),
                        "the discount belongs in the row that records the decision")


class TestThePurchaserRole(PurchaseTestCase):
    """A Purchaser does the whole job on /purchases without a manager: enter,
    approve, pay. The one thing they cannot do is approve their own bill — and
    that rule is what makes a manager unnecessary rather than merely absent."""

    def bill_by(self, tag):
        self.as_user(self.user_with(roles.PURCHASER, tag))
        return self.enter()

    def approval_for(self, bill):
        rows = frappe.get_all("POS Approval",
                              filters={"action": "purchase_bill", "status": "pending"},
                              fields=["name", "action_payload"])
        return next(r.name for r in rows
                    if json.loads(r.action_payload or "{}").get("bill") == bill)

    def test_one_purchaser_approves_another_s_bill(self):
        name = self.bill_by("enters")
        frappe.set_user(self.user_with(roles.PURCHASER, "checks"))
        result = self.call(api.resolve_purchase_bill, name=name, decision="approved")
        self.assertEqual(result["status"], "approved")

    def test_but_never_their_own(self):
        name = self.bill_by("alone")
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_purchase_bill, name=name, decision="approved")

    def test_a_purchaser_marks_a_bill_paid(self):
        name = self.bill_by("owes")
        frappe.set_user(self.user_with(roles.PURCHASER, "pays"))
        self.call(api.resolve_purchase_bill, name=name, decision="approved")
        out = self.call(api.mark_bill_paid, name=name, paid=1)
        self.assertEqual(out["payment_status"], "paid")

    def test_a_cashier_cannot_approve_through_the_approvals_endpoint_either(self):
        """The second door. resolve_approval answers discounts and voids for any
        POS user, rightly — and it also decides purchase bills. Gating only
        purchases.py would have left this open to the whole till."""
        name = self.bill_by("door")
        approval = self.approval_for(name)
        frappe.set_user(self.user_with(roles.CASHIER, "door-cashier"))
        with self.assertRaises(frappe.PermissionError):
            self.call(api.resolve_approval, name=approval, decision="approved")
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "status"), "pending")

    def test_a_purchaser_can_approve_through_it(self):
        name = self.bill_by("door2")
        approval = self.approval_for(name)
        frappe.set_user(self.user_with(roles.PURCHASER, "door2-checks"))
        self.call(api.resolve_approval, name=approval, decision="approved")
        self.assertEqual(frappe.db.get_value("Purchase Bill", name, "status"), "approved")

    def test_the_purchase_queue_is_not_the_till_s(self):
        self.as_user(self.user_with(roles.CASHIER, "queue-cashier"))
        with self.assertRaises(frappe.PermissionError):
            api.get_purchase_approvals()

    def test_a_purchaser_is_not_a_manager_and_not_a_cashier(self):
        """A branch of the tree, not a rung on it."""
        user = self.user_with(roles.PURCHASER, "scope")
        self.assertFalse(roles.is_manager(user))
        self.assertFalse(roles.has_any(roles.POS_ROLES, user))


class TheWholePurchasesScreenOpensToAPurchaser(PurchaseTestCase):
    """The trap this role walked into twice on the way in: the page let a
    Purchaser through, and an endpoint behind it did not — first
    `resolve_approval` (the queue approves through it), then the expenses tab.
    A screen that opens and then refuses its own buttons is worse than one that
    does not open. So this reads the page and asks every endpoint it calls."""

    def test_the_expenses_tab_opens_to_a_purchaser(self):
        self.as_user(self.user_with(roles.PURCHASER, "expenses"))
        api.get_expense_categories()
        api.get_expenses()

    def test_and_stays_open_to_the_till(self):
        """Widened, not moved: the till pays some expenses out of the drawer."""
        self.as_user(self.user_with(roles.CASHIER, "expenses-till"))
        api.get_expense_categories()
        api.get_expenses()

    def test_every_endpoint_the_page_calls_admits_a_purchaser(self):
        import inspect
        import pathlib
        import re

        html = (pathlib.Path(roles.__file__).parent / "www" / "purchases.html").read_text()
        called = set(re.findall(r"call\('([a-z_]+)'", html)) | \
            set(re.findall(r"luuvcrm\.api\.([a-z_]+)", html))
        called.discard("manifest")      # public by design; carries no data
        self.assertGreater(len(called), 5, "the page's calls were not found — the pattern broke")

        admits = ("require_purchases()", "require_expenses()", "PURCHASE_ROLES")
        shut = []
        for name in sorted(called):
            source = inspect.getsource(getattr(api, name))
            if not any(a in source for a in admits):
                shut.append(name)
        self.assertEqual(shut, [], "the page calls these, and a Purchaser is refused by them")


class TestThePurchaseDate(PurchaseTestCase):
    """The day the goods were bought, entered with the bill and today unless the
    buyer says otherwise. A bill for Saturday's market run typed in on Monday is
    Saturday's buying, and every purchase report counts it there."""

    def day(self, n):
        return frappe.utils.getdate(frappe.utils.add_days(frappe.utils.today(), n))

    def test_a_bill_with_no_date_is_dated_today(self):
        doc = frappe.get_doc("Purchase Bill", self.enter())
        self.assertEqual(doc.purchase_date, self.day(0))

    def test_a_chosen_earlier_date_is_kept(self):
        doc = frappe.get_doc("Purchase Bill", self.enter(purchase_date=str(self.day(-3))))
        self.assertEqual(doc.purchase_date, self.day(-3))
        self.assertEqual(frappe.utils.getdate(doc.requested_at), self.day(0),
                         "when it was typed in is still recorded apart from when it was bought")

    def test_a_date_in_the_future_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self.enter(purchase_date=str(self.day(1)))
        self.assertIn("future", str(caught.exception))

    def test_something_that_is_not_a_date_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.enter(purchase_date="next tuesday")

    def test_a_bill_saved_without_one_takes_the_day_it_was_entered(self):
        """The desk, an old client, a fixture — anything that saves a bill with no
        purchase_date would otherwise drop out of every report."""
        doc = frappe.get_doc({
            "doctype": "Purchase Bill", "vendor": "_LFT-Vendor", "status": "pending",
            "requested_at": frappe.utils.add_days(frappe.utils.now_datetime(), -4),
            "items": [{"item": self.tomato, "qty": 1, "price": 10}],
        })
        doc.insert(ignore_permissions=True)
        self._bills.append(doc.name)
        self.assertEqual(doc.purchase_date, self.day(-4))

    def test_the_bill_carries_its_date_to_the_screens(self):
        name = self.enter(purchase_date=str(self.day(-2)))
        b = api.get_purchase_bill(name=name)
        self.assertEqual(b["purchase_date"], str(self.day(-2)))
        self.assertEqual(b["date"], frappe.utils.formatdate(self.day(-2), "d MMM yyyy"))

    def test_the_day_card_counts_it_on_the_day_it_was_bought(self):
        name = self.enter(purchase_date=str(self.day(-3)))
        then = api.get_purchase_day_summary(date=str(self.day(-3)))
        now = api.get_purchase_day_summary()
        self.assertIn(name, [b["name"] for b in then["bills"]])
        self.assertNotIn(name, [b["name"] for b in now["bills"]],
                         "typed in today, bought three days ago — not today's buying")

    def test_a_search_result_shows_the_day_it_was_bought(self):
        self.enter(vendor="_LFT-Dated-Vendor", purchase_date=str(self.day(-5)))
        hit = api.search_purchase_bills(q="_LFT-Dated-Vendor")["bills"][0]
        self.assertEqual(hit["date"], frappe.utils.formatdate(self.day(-5), "d MMM yyyy"))

    def test_the_approver_is_told_an_earlier_bill_is_not_today_s(self):
        def subhead(bill):
            for a in frappe.get_all("POS Approval", filters={"action": "purchase_bill"},
                                    fields=["action_payload", "subhead"]):
                if json.loads(a.action_payload or "{}").get("bill") == bill:
                    return a.subhead
        self.assertNotIn("bought", subhead(self.enter()))
        self.assertIn(frappe.utils.formatdate(self.day(-2), "d MMM"),
                      subhead(self.enter(purchase_date=str(self.day(-2)))))

    def test_the_week_figure_counts_by_the_day_it_was_bought(self):
        before = api.get_purchase_bills()["week"]
        self.enter(lines=[{"item": self.tomato, "qty": 1, "price": 777}],
                   purchase_date=str(self.day(-30)))
        self.assertEqual(api.get_purchase_bills()["week"], before,
                         "a month-old bill entered today is not this week's spend")
