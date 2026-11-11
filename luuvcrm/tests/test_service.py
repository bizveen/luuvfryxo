"""The service floor: the cash drawer, and an order's life after it is taken.

Covers what happens between placing a bill and paying it — cancelling, moving a
table, the kitchen advancing a ticket — plus the drawer movements that make the
end-of-day count mean anything.

The drawer is the part with real money in it. A pay-out that is not recorded is
simply cash missing at close, and nobody can say whether it was a supplier
payment or a theft.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles
from luuvcrm.tests import fixtures as fx


class FloorTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.burger = fx.item(self, "SV-BURGER")
        fx.item_price(self, self.burger, 1000)
        fx.service_charge(self, 0)
        self._orders = []
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            if self._orders:
                frappe.db.delete("Comment", {
                    "reference_doctype": "POS Order",
                    "reference_name": ["in", self._orders],
                    "subject": api.CUSTOMER_FEEDBACK_SUBJECT,
                })
            for name in self._orders:
                if not frappe.db.exists("POS Order", name):
                    continue
                try:
                    doc = frappe.get_doc("POS Order", name)
                    if doc.docstatus in (1, 2):
                        doc.db_set("docstatus", 0)
                    frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
                except Exception:
                    frappe.db.rollback()
        finally:
            frappe.set_user(user)

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def order(self, qty=1, **kwargs):
        result = self.call(api.send_to_kitchen,
                           items=json.dumps([{"item": self.burger, "qty": qty}]),
                           order_type="Dine In", **kwargs)
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        return frappe.get_doc("POS Order", name)

    def table(self):
        # The test site's Administrator may have an open shift at one of
        # several branches. Pick a table from the same branch as the order the
        # fixture creates, not whichever table the database returns first.
        here = branch.current()
        name = frappe.db.get_value("Restaurant Table",
                                   {"restaurant": here}, "name") if here else None
        if not name:
            self.skipTest("no Restaurant Table in the active branch on this site")
        return name


class TestCancellingAnOrder(FloorTestCase):
    def test_an_unpaid_order_can_be_cancelled(self):
        order = self.order()
        self.call(api.cancel_order, order_name=order.name, reason="customer left")
        order.reload()
        self.assertEqual(order.docstatus, 2)
        self.assertEqual(order.kitchen_status, "Cancelled")

    def test_a_paid_order_cannot_be_cancelled(self):
        """Once money has changed hands the bill is history — a correction goes
        through the invoice, not by voiding the ticket underneath it."""
        order = self.order()
        self.call(api.process_payment, order_name=order.name)
        fx.drop_invoice(self, frappe.db.get_value("POS Order", order.name, "pos_invoice"))
        order.reload()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.cancel_order, order_name=order.name)
        self.assertIn("payment already processed", str(caught.exception))

    def test_cancelling_needs_an_order(self):
        with self.assertRaises(frappe.ValidationError):
            self.call(api.cancel_order)

    def test_a_manager_approved_void_cancels_the_order(self):
        """The other way a bill dies: the cashier cannot cancel it themselves, so
        they raise a void and a manager approves it — and the approval's own
        action is what cancels the order. Brought over from `main`'s suite, which
        is the only place this path was covered.
        """
        from luuvcrm import roles

        order = self.order()
        approval = self.call(api.create_approval, approval_type="void", action="void",
                             pos_order=order.name, title="Void table 2",
                             action_payload="{}")
        self.addCleanup(lambda: frappe.db.exists("POS Approval", approval["name"]) and
                        frappe.delete_doc("POS Approval", approval["name"], force=True,
                                          ignore_permissions=True))

        # A different person has to answer it — block_self_approval() is the rule
        # and it is tested in test_approvals; here it just has to be satisfied.
        email = f"lf-test-void-{frappe.scrub(order.get(branch.FIELD) or 'branch')}@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Void Test",
                            "send_welcome_email": 0,
                            "roles": [{"role": roles.RESTAURANT_MANAGER}]}
                           ).insert(ignore_permissions=True)
        restaurant = order.get(branch.FIELD)
        if restaurant and not frappe.db.exists("User Permission", {
                "user": email, "allow": "Restaurant", "for_value": restaurant}):
            frappe.get_doc({"doctype": "User Permission", "user": email,
                            "allow": "Restaurant", "for_value": restaurant,
                            "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        try:
            self.call(api.resolve_approval, name=approval["name"], decision="approved")
        finally:
            frappe.set_user(previous)

        order.reload()
        self.assertEqual(order.docstatus, 2)
        self.assertEqual(order.kitchen_status, "Cancelled")
        self.assertEqual(frappe.db.get_value("POS Approval", approval["name"], "status"),
                         "approved")

    def test_the_cancellation_is_written_into_the_history(self):
        """The manager's activity trail reads this back — a bill that vanished
        with no record is the thing the trail exists to prevent."""
        order = self.order()
        self.call(api.cancel_order, order_name=order.name, reason="wrong table")
        versions = frappe.get_all("Version", filters={"ref_doctype": "POS Order",
                                                      "docname": order.name})
        self.assertTrue(versions, "a cancellation must leave a trace")


class TestDiscardingAnEmptyTicket(FloorTestCase):
    """The POS opens a ticket as soon as the cashier taps an item, so clearing the
    cart has to clean up after itself — but only when nothing real happened."""

    def test_a_ticket_nothing_was_fired_from_is_deleted(self):
        order = self.order()
        result = self.call(api.delete_draft_order, order_name=order.name)
        self.assertTrue(result["deleted"])
        self.assertFalse(frappe.db.exists("POS Order", order.name))

    def test_a_ticket_the_kitchen_has_seen_is_kept(self):
        order = self.order()
        frappe.db.set_value("POS Order Item", order.items[0].name, "sent_qty", 1)
        result = self.call(api.delete_draft_order, order_name=order.name)
        self.assertFalse(result["deleted"])
        self.assertTrue(frappe.db.exists("POS Order", order.name),
                        "food already cooking must leave a ticket behind")

    def test_a_paid_ticket_is_kept(self):
        order = self.order()
        self.call(api.process_payment, order_name=order.name)
        fx.drop_invoice(self, frappe.db.get_value("POS Order", order.name, "pos_invoice"))
        self.assertFalse(self.call(api.delete_draft_order, order_name=order.name)["deleted"])

    def test_deleting_something_that_is_already_gone_is_not_an_error(self):
        result = self.call(api.delete_draft_order, order_name="_LFT-GONE")
        self.assertEqual(result, {"ok": True, "deleted": False})


class TestMovingATable(FloorTestCase):
    def test_an_open_order_moves(self):
        order = self.order()
        table = self.table()
        self.call(api.move_order_table, order_name=order.name, table=table)
        order.reload()
        self.assertEqual(order.restaurant_table, table)

    def test_it_will_not_move_to_a_table_that_does_not_exist(self):
        order = self.order()
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.move_order_table, order_name=order.name, table="_LFT-NO-TABLE")
        self.assertIn("not found", str(caught.exception))

    def test_a_paid_order_does_not_move(self):
        order = self.order()
        self.call(api.process_payment, order_name=order.name)
        fx.drop_invoice(self, frappe.db.get_value("POS Order", order.name, "pos_invoice"))
        order.reload()
        with self.assertRaises(frappe.ValidationError):
            self.call(api.move_order_table, order_name=order.name, table=self.table())


class TestTheKitchenTicket(FloorTestCase):
    """Pending → Processing → Ready → Served. The waiter's screen reads this, so
    a status that does not move is a plate nobody collects."""

    def test_a_new_ticket_starts_pending(self):
        self.assertEqual(self.order().kitchen_status, "Pending")

    def test_the_kitchen_starts_cooking(self):
        order = self.order()
        self.call(api.mark_kitchen_processing, order_name=order.name)
        order.reload()
        self.assertEqual(order.kitchen_status, "Processing")

    def test_the_kitchen_calls_it_ready(self):
        order = self.order()
        self.call(api.mark_kitchen_ready, order_name=order.name)
        order.reload()
        self.assertEqual(order.kitchen_status, "Ready")

    def test_an_online_order_can_be_ready_before_its_kot(self):
        order = self.order(draft=1)
        order.db_set("order_source", "Online")
        order.db_set("waiter_name", "Kiosk")
        self.call(api.mark_kitchen_ready, order_name=order.name)
        order.reload()
        self.assertEqual(order.kitchen_status, "Ready")

    def app_order(self):
        order = self.order(draft=1)
        order.db_set("order_source", "Online")
        order.db_set("waiter_name", "CUS")
        return order

    def test_confirming_an_app_order_releases_it_without_firing_the_kot(self):
        order = self.app_order()

        result = self.call(api.confirm_app_order, order_name=order.name)

        order.reload()
        self.assertTrue(result["confirmed"])
        self.assertEqual(order.kitchen_status, "Pending")
        self.assertEqual(int(order.items[0].sent_qty or 0), 0)

    def test_an_app_order_cannot_fire_its_kot_before_confirmation(self):
        order = self.app_order()

        with self.assertRaises(frappe.ValidationError):
            self.call(api.mark_kot_printed, order_name=order.name)

        order.reload()
        self.assertEqual(order.kitchen_status, "Draft")
        self.assertEqual(int(order.items[0].sent_qty or 0), 0)

    def test_an_app_order_cannot_be_ready_before_confirmation(self):
        order = self.app_order()

        with self.assertRaises(frappe.ValidationError):
            self.call(api.mark_kitchen_ready, order_name=order.name)

        self.call(api.confirm_app_order, order_name=order.name)
        self.call(api.mark_kitchen_ready, order_name=order.name)
        order.reload()
        self.assertEqual(order.kitchen_status, "Ready")

    def test_app_order_confirmation_is_visible_to_the_customer_session(self):
        mobile = "0770000871"
        fx.customer_account(self, mobile, name="App Customer")
        token = fx.customer_token(self, mobile)
        order = self.app_order()
        order.db_set("mobile", mobile)

        self.call(api.confirm_app_order, order_name=order.name)
        result = self.call(api.customer_get_orders, token=token)

        shown = next(row for row in result["orders"] if row["name"] == order.name)
        self.assertEqual(shown["status"], "Pending")

    def test_cancelled_unconfirmed_app_order_is_visible_to_the_customer_session(self):
        mobile = "0770000875"
        fx.customer_account(self, mobile, name="Cancelled App Customer")
        token = fx.customer_token(self, mobile)
        order = self.app_order()
        order.db_set("mobile", mobile)

        self.call(api.cancel_order, order_name=order.name,
                  reason="cancelled before confirmation")
        result = self.call(api.customer_get_orders, token=token)

        shown = next(row for row in result["orders"] if row["name"] == order.name)
        self.assertEqual(shown["status"], "Cancelled")
        self.assertEqual(shown["docstatus"], 2)

    def test_customer_can_review_their_completed_order_once(self):
        mobile = "0770000872"
        fx.customer_account(self, mobile, name="Review Customer")
        token = fx.customer_token(self, mobile)
        order = self.order()
        order.db_set("mobile", mobile)
        order.db_set("customer_name", "Review Customer")
        self.call(api.mark_order_served, order_name=order.name)

        first = self.call(
            api.customer_submit_order_feedback,
            token=token,
            order_name=order.name,
            rating=5,
            comment="Fast service & crispy food",
        )
        second = self.call(
            api.customer_submit_order_feedback,
            token=token,
            order_name=order.name,
            rating=4,
            comment="duplicate",
        )

        self.assertTrue(first["success"])
        self.assertTrue(second["already_submitted"])
        self.assertEqual(frappe.db.count("Comment", {
            "reference_doctype": "POS Order",
            "reference_name": order.name,
            "subject": api.CUSTOMER_FEEDBACK_SUBJECT,
        }), 1)
        shown = next(row for row in self.call(
            api.customer_get_orders, token=token)["orders"]
                     if row["name"] == order.name)
        self.assertTrue(shown["feedback_submitted"])
        self.assertEqual(shown["feedback_rating"], 5)

    def test_customer_cannot_review_an_open_or_somebody_elses_order(self):
        owner_mobile = "0770000873"
        other_mobile = "0770000874"
        fx.customer_account(self, owner_mobile, name="Order Owner")
        fx.customer_account(self, other_mobile, name="Other Customer")
        owner_token = fx.customer_token(self, owner_mobile)
        other_token = fx.customer_token(self, other_mobile)
        order = self.order()
        order.db_set("mobile", owner_mobile)

        open_result = self.call(
            api.customer_submit_order_feedback, token=owner_token,
            order_name=order.name, rating=5)
        self.assertFalse(open_result["success"])

        self.call(api.mark_order_served, order_name=order.name)
        other_result = self.call(
            api.customer_submit_order_feedback, token=other_token,
            order_name=order.name, rating=5)
        self.assertFalse(other_result["success"])

    def test_the_waiter_marks_it_served(self):
        order = self.order()
        self.call(api.mark_order_served, order_name=order.name)
        order.reload()
        self.assertEqual(order.kitchen_status, "Served")

    def test_serving_closes_the_ticket(self):
        """Serving submits the order, which is what takes it off every open-order
        screen at once."""
        order = self.order()
        self.call(api.mark_order_served, order_name=order.name)
        order.reload()
        self.assertEqual(order.docstatus, 1)

    def test_serving_twice_is_refused(self):
        """The endpoint's own "already served" branch is unreachable, because
        serving submits the order and the docstatus check above it fires first.
        Either way the second tap is refused, which is what the waiter needs."""
        order = self.order()
        self.call(api.mark_order_served, order_name=order.name)
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.mark_order_served, order_name=order.name)
        self.assertIn("not in draft state", str(caught.exception))

    def test_the_kitchen_cannot_serve_a_closed_ticket_either(self):
        order = self.order()
        self.call(api.mark_kitchen_served, order_name=order.name)
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.mark_kitchen_served, order_name=order.name)
        self.assertIn("already served", str(caught.exception))

    def test_every_kitchen_step_needs_an_order(self):
        for endpoint in (api.mark_kitchen_processing, api.mark_kitchen_ready,
                         api.mark_kitchen_served, api.mark_order_served):
            with self.subTest(endpoint=endpoint.__name__):
                with self.assertRaises(frappe.ValidationError):
                    self.call(endpoint)

    def test_a_fired_ticket_is_recorded_as_printed(self):
        order = self.order()
        self.call(api.mark_kot_printed, order_name=order.name)
        order.reload()
        self.assertEqual(int(order.items[0].sent_qty), 1,
                         "the kitchen has this line now; a reprint must not fire it again")
        self.assertEqual(order.kitchen_status, "Processing",
                         "printing the KOT is the cooking start without a kitchen screen")

    def test_app_order_starts_cooking_when_its_confirmed_kot_is_printed(self):
        order = self.app_order()
        self.call(api.confirm_app_order, order_name=order.name)

        self.call(api.mark_kot_printed, order_name=order.name)

        order.reload()
        self.assertEqual(order.kitchen_status, "Processing")


class TestWhatTheScreensShow(FloorTestCase):
    def test_an_open_order_appears_on_its_table(self):
        order = self.order()
        table = self.table()
        self.call(api.move_order_table, order_name=order.name, table=table)
        names = [o["name"] for o in self.call(api.get_table_orders, table=table)["orders"]]
        self.assertIn(order.name, names)

    def test_a_paid_order_leaves_the_table(self):
        order = self.order()
        table = self.table()
        self.call(api.move_order_table, order_name=order.name, table=table)
        self.call(api.process_payment, order_name=order.name)
        fx.drop_invoice(self, frappe.db.get_value("POS Order", order.name, "pos_invoice"))
        names = [o["name"] for o in self.call(api.get_table_orders, table=table)["orders"]]
        self.assertNotIn(order.name, names, "a settled table must read as free")

    def test_a_waiter_sees_their_own_order(self):
        order = self.order()
        self.assertIn(order.name, [o["name"] for o in self.call(api.get_my_orders)["orders"]])

    def test_the_orders_tab_is_shared_between_waiters(self):
        order = self.order()
        frappe.db.set_value("POS Order", order.name, "owner", "another.waiter@example.com")
        rows = self.call(api.get_my_orders)["orders"]
        mine = [o for o in rows if o["name"] == order.name]
        self.assertEqual(len(mine), 1)
        self.assertIn("waiter_name", mine[0])

    def test_the_kitchen_sees_a_new_ticket(self):
        order = self.order()
        names = [o["name"] for o in self.call(api.get_kitchen_orders)["orders"]]
        self.assertIn(order.name, names)

    def test_a_cancelled_order_leaves_the_kitchen(self):
        order = self.order()
        self.call(api.cancel_order, order_name=order.name, reason="void")
        names = [o["name"] for o in self.call(api.get_kitchen_orders)["orders"]]
        self.assertNotIn(order.name, names)

    def test_tables_come_back_with_their_state(self):
        result = self.call(api.get_tables_with_status)
        self.assertIn("tables", result)


class TestTheCashDrawer(FrappeTestCase):
    """Money in and out of the drawer that is not a sale. Without these the
    end-of-day count cannot be reconciled to anything."""

    def setUp(self):
        super().setUp()
        api._install_cash_movement()
        self.profile = frappe.db.get_value("POS Profile", {}, "name")
        api._ensure_open_shift(self.profile)
        self._moves = []
        self.addCleanup(self._drop_moves)

    def _drop_moves(self):
        for name in self._moves:
            if frappe.db.exists("POS Cash Movement", name):
                frappe.delete_doc("POS Cash Movement", name, force=True, ignore_permissions=True)

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def move(self, direction, amount, reason="test"):
        before = set(frappe.get_all("POS Cash Movement", pluck="name"))
        result = self.call(api.record_cash_movement, direction=direction,
                           amount=amount, reason=reason)
        self._moves += list(set(frappe.get_all("POS Cash Movement", pluck="name")) - before)
        return result

    def test_cash_paid_in_is_recorded(self):
        before = self.call(api.get_cash_movements)["in_total"]
        self.move("in", 500, "float top-up")
        self.assertEqual(self.call(api.get_cash_movements)["in_total"], before + 500)

    def test_cash_paid_out_is_recorded(self):
        before = self.call(api.get_cash_movements)["out_total"]
        self.move("out", 1200, "paid the vegetable supplier")
        self.assertEqual(self.call(api.get_cash_movements)["out_total"], before + 1200)

    def test_in_and_out_are_kept_apart(self):
        movements = self.call(api.get_cash_movements)
        before_in, before_out = movements["in_total"], movements["out_total"]
        self.move("in", 100)
        self.move("out", 40)
        after = self.call(api.get_cash_movements)
        self.assertEqual(after["in_total"], before_in + 100)
        self.assertEqual(after["out_total"], before_out + 40)

    def test_the_reason_is_kept(self):
        """A pay-out with no reason is indistinguishable from cash going
        missing, which is the whole point of recording it."""
        self.move("out", 300, "staff advance")
        latest = self.call(api.get_cash_movements)["moves"][0]
        self.assertEqual(latest["reason"], "staff advance")
        self.assertEqual(latest["amount"], 300)

    def test_only_in_or_out_is_a_direction(self):
        for direction in ("sideways", "", None, "IN OUT"):
            with self.subTest(direction=direction):
                with self.assertRaises(frappe.ValidationError) as caught:
                    self.call(api.record_cash_movement, direction=direction, amount=100)
                self.assertIn("Direction must be", str(caught.exception))

    def test_direction_is_read_case_insensitively(self):
        self.move("IN", 50)  # must not raise

    def test_a_movement_of_nothing_is_refused(self):
        for amount in (0, -100):
            with self.subTest(amount=amount):
                with self.assertRaises(frappe.ValidationError) as caught:
                    self.call(api.record_cash_movement, direction="in", amount=amount)
                self.assertIn("greater than zero", str(caught.exception))

    def test_the_newest_movement_is_first(self):
        self.move("in", 11, "older")
        self.move("in", 22, "newer")
        self.assertEqual(self.call(api.get_cash_movements)["moves"][0]["reason"], "newer")

    def test_kitchen_staff_cannot_touch_the_drawer(self):
        email = "lf-test-sv-kitchen@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Kitchen",
                            "send_welcome_email": 0,
                            "roles": [{"role": roles.KITCHEN_STAFF}]}).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        with self.assertRaises(frappe.PermissionError):
            self.call(api.record_cash_movement, direction="out", amount=100)
        with self.assertRaises(frappe.PermissionError):
            self.call(api.get_cash_movements)


class TestTheKitchenQueueCannotSwallowANewTicket(FloorTestCase):
    """The failure this class exists for was found by two tests that had been
    failing for days and being explained away as "test-site noise".

    The kitchen list filtered on status alone, ordered oldest-first, and cut at
    `SCREEN_ORDER_CEILING`. Once more tickets than that had piled up unserved —
    275 against a ceiling of 200 on the test site — the screen filled with
    ancient ones and **the order that had just been fired never appeared**.
    Nothing anywhere said so. Older versions also left paid tickets open, so
    over months the ones nobody explicitly served accumulated.
    """

    def stale_pile(self, n=3):
        """Tickets from before the window: old, unserved, and never coming back."""
        from frappe.utils import add_to_date

        made = []
        for _ in range(n):
            order = self.order()
            frappe.db.set_value("POS Order", order.name, "creation",
                                add_to_date(frappe.utils.now_datetime(), days=-9),
                                update_modified=False)
            made.append(order.name)
        return made

    def test_a_ticket_fired_now_is_on_the_screen_behind_a_backlog(self):
        self.stale_pile()
        fresh = self.order()
        names = [o["name"] for o in self.call(api.get_kitchen_orders)["orders"]]
        self.assertIn(fresh.name, names)

    def test_a_ticket_older_than_the_window_is_not_shown(self):
        """It is not work the kitchen is about to do. It is counted, not
        rendered."""
        old = self.stale_pile(1)[0]
        result = self.call(api.get_kitchen_orders)
        self.assertNotIn(old, [o["name"] for o in result["orders"]])

    def test_what_is_hidden_is_counted_and_said(self):
        """The whole failure was silence, so the count is part of the payload
        and the screen prints it."""
        self.stale_pile(2)
        result = self.call(api.get_kitchen_orders)
        for key in ("shown", "waiting", "truncated", "stale", "window_hours"):
            self.assertIn(key, result)
        self.assertGreaterEqual(result["stale"], 2)

    def test_the_screen_prints_that_line(self):
        html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "kitchen.html"))
        self.assertIn("queueNotice", html)
        self.assertIn("showQueueNotice", html)

    def test_the_window_is_wide_enough_for_a_late_shift(self):
        """A shift that starts at 6pm and ends at 2am must not lose half its
        tickets at midnight, which is what "today" would have done."""
        from luuvcrm.api.kitchen import KITCHEN_WINDOW_HOURS

        self.assertGreaterEqual(KITCHEN_WINDOW_HOURS, 24)

    def test_full_payment_closes_the_customer_ticket(self):
        """The current no-kitchen-screen flow ends at payment, which must also
        release the completion/review state in the customer app."""
        order = self.order()
        self.call(api.process_payment, order_name=order.name)
        fx.drop_invoice(self, frappe.db.get_value("POS Order", order.name, "pos_invoice"))
        status, docstatus = frappe.db.get_value(
            "POS Order", order.name, ["kitchen_status", "docstatus"])
        self.assertEqual(status, "Served")
        self.assertEqual(docstatus, 1)
