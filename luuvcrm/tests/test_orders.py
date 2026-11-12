"""The order lifecycle, end to end.

These drive the real endpoints through `frappe.local.form_dict`, the way a
request does, so they cover the argument parsing and the guards as well as the
arithmetic. This is the safety net for Phase 2b onwards: the three POS pages are
about to be collapsed into one and `api.py` split into a package, and nothing
else would notice if a total came out wrong afterwards.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles
from luuvcrm.tests import fixtures as fx


class OrderTestCase(FrappeTestCase):
    """A priced menu and a POS-capable session."""

    def setUp(self):
        super().setUp()
        self.burger = fx.item(self, "ORD-BURGER")
        fx.item_price(self, self.burger, 1000)
        self.cola = fx.item(self, "ORD-COLA")
        fx.item_price(self, self.cola, 250)
        fx.service_charge(self, 10)
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        for name in getattr(self, "_orders", []):
            if frappe.db.exists("POS Order", name):
                frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
        for name in getattr(self, "_tables", []):
            if frappe.db.exists("Restaurant Table", name):
                frappe.delete_doc(
                    "Restaurant Table", name, force=True, ignore_permissions=True
                )

    def call(self, endpoint, **kwargs):
        """Invoke an endpoint the way a request would."""
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def order(self, items, order_type="Dine In", **kwargs):
        result = self.call(api.send_to_kitchen, items=json.dumps(items),
                           order_type=order_type, **kwargs)
        name = result["name"] if isinstance(result, dict) else result
        self._orders = getattr(self, "_orders", []) + [name]
        return frappe.get_doc("POS Order", name)


class TestOrderTotals(OrderTestCase):
    def test_a_dine_in_order_carries_the_service_charge(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        self.assertEqual(order.grand_total, 2200, "2000 of food plus 10% service")
        self.assertEqual(order.service_charge_amount, 200)
        self.assertEqual(order.service_charge_rate, 10)

    def test_a_take_away_order_carries_none(self):
        order = self.order([{"item": self.burger, "qty": 2}], order_type="Take Away")
        self.assertEqual(order.grand_total, 2000)
        self.assertEqual(order.service_charge_amount, 0)

    def test_a_parcelled_line_is_excluded_from_the_service_charge(self):
        """A dine-in table that also takes one burger home: service charge
        applies to what is eaten in, not to the parcel."""
        order = self.order([
            {"item": self.burger, "qty": 1},
            {"item": self.burger, "qty": 1, "takeaway": 1},
        ])
        api._apply_takeaway_service_charge(order)
        order.reload()
        self.assertEqual(order.service_charge_amount, 100, "10% of the one dine-in burger")
        self.assertEqual(order.grand_total, 2100)

    def test_quantities_multiply(self):
        order = self.order([{"item": self.burger, "qty": 3}, {"item": self.cola, "qty": 2}])
        self.assertEqual(sum(i.qty * i.rate for i in order.items), 3500)

    def test_a_quantity_below_one_is_treated_as_one(self):
        """max(qty, 1) — a zero or negative quantity must never make a line free
        or, worse, negative."""
        order = self.order([{"item": self.burger, "qty": 0}])
        self.assertEqual(order.items[0].qty, 1)


class TestPricesIgnoreTheClient(OrderTestCase):
    """S2, driven through the endpoint rather than asserted from the source."""

    def test_a_posted_rate_is_discarded(self):
        order = self.order([{"item": self.burger, "qty": 1, "rate": 1}])
        self.assertEqual(order.items[0].rate, 1000,
                         "the price list is the authority, not the request")
        self.assertEqual(order.grand_total, 1100)

    def test_a_posted_rate_cannot_inflate_a_line_either(self):
        order = self.order([{"item": self.burger, "qty": 1, "rate": 99999}])
        self.assertEqual(order.items[0].rate, 1000)

    def test_an_omitted_rate_is_filled_in(self):
        order = self.order([{"item": self.cola, "qty": 1}])
        self.assertEqual(order.items[0].rate, 250)


class TestOrderValidation(OrderTestCase):
    def test_an_empty_order_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.call(api.send_to_kitchen, items=json.dumps([]))

    def test_update_order_needs_a_name(self):
        with self.assertRaises(frappe.ValidationError):
            self.call(api.update_order, items=json.dumps([{"item": self.burger, "qty": 1}]))

    def test_a_draft_order_starts_unpaid_and_unfired(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        self.assertEqual(order.docstatus, 0)
        self.assertFalse(order.pos_invoice)
        self.assertEqual(int(order.items[0].get("paid_qty") or 0), 0)


class TestCustomerRegistrationFlags(OrderTestCase):
    def test_the_pos_distinguishes_a_registered_number_from_a_guest(self):
        from luuvcrm.api.orders import _tag_registered_customers

        fx.customer_account(self, "0770000861")
        rows = [
            frappe._dict(mobile="+94770000861"),
            frappe._dict(mobile="0770000862"),
        ]
        _tag_registered_customers(rows)
        self.assertTrue(rows[0].customer_registered)
        self.assertFalse(rows[1].customer_registered)


class TestCustomerAppCheckoutIdentity(OrderTestCase):
    MOBILE = "0770000863"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE, name="Verified Customer")
        self.token = fx.customer_token(self, self.MOBILE)

    def checkout(self, **kwargs):
        result = self.call(
            api.kiosk_place_order,
            items=json.dumps([{"item": self.burger, "qty": 1}]),
            order_channel="CUS",
            order_type="Take Away",
            **kwargs,
        )
        if result.get("name"):
            self._orders = getattr(self, "_orders", []) + [result["name"]]
        return result

    def test_a_signed_in_checkout_uses_the_tokens_verified_mobile(self):
        result = self.checkout(
            mobile="0779999999",
            customer_token=self.token,
        )

        self.assertEqual(
            frappe.db.get_value("POS Order", result["name"], "mobile"),
            self.MOBILE,
        )

    def test_an_expired_session_cannot_be_used_as_a_checkout_identity(self):
        result = self.checkout(
            mobile=self.MOBILE,
            customer_token="not-a-real-session",
        )

        self.assertEqual(result["error"], "Your session expired — sign in again")
        self.assertNotIn("name", result)


class TestUpdateOrder(OrderTestCase):
    def test_it_rebuilds_the_lines_and_the_total(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 1},
                                    {"item": self.cola, "qty": 2}]))
        order.reload()
        self.assertEqual(len(order.items), 2)
        self.assertEqual(order.grand_total, 1650, "1500 of food plus 10% service")

    def test_it_keeps_what_the_kitchen_already_cooked(self):
        """sent_qty survives the rebuild, so a KOT reprint fires only new lines."""
        order = self.order([{"item": self.burger, "qty": 2}])
        frappe.db.set_value("POS Order Item", order.items[0].name, "sent_qty", 2)
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 3}]))
        order.reload()
        self.assertEqual(int(order.items[0].sent_qty), 2,
                         "the two already fired must not be fired again")

    def test_sent_qty_never_exceeds_the_line(self):
        """Reducing a line below what was already sent must clamp, not go negative."""
        order = self.order([{"item": self.burger, "qty": 3}])
        frappe.db.set_value("POS Order Item", order.items[0].name, "sent_qty", 3)
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 1}]))
        order.reload()
        self.assertEqual(int(order.items[0].sent_qty), 1)

    def test_it_keeps_the_waiter_note_the_cashier_does_not_resend(self):
        """The POS does not send notes back; without this a cashier autosave would
        wipe 'no chilli' off the kitchen ticket."""
        order = self.order([{"item": self.burger, "qty": 1, "notes": "no chilli"}])
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 2}]))
        order.reload()
        self.assertEqual(order.items[0].notes, "no chilli")

    def test_the_table_swap_race_is_refused(self):
        """The client says which table it thinks the order is on. If it has moved,
        refuse the rebuild rather than silently mixing two tickets."""
        order = self.order([{"item": self.burger, "qty": 1}])
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.update_order, order_name=order.name,
                      expected_table="_LFT-Some-Other-Table",
                      items=json.dumps([{"item": self.burger, "qty": 5}]))
        self.assertIn("order-table-mismatch", str(caught.exception),
                      "the POS matches on this marker to reset and refresh")
        order.reload()
        self.assertEqual(order.items[0].qty, 1, "the order must be untouched")


class TestWaiterAppendItems(OrderTestCase):
    """Appending is deliberately not update_order's full rebuild: it must leave
    fired lines alone so a KOT reprint shows only what is new."""

    def test_new_lines_are_added_without_disturbing_the_old(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        frappe.db.set_value("POS Order Item", order.items[0].name, "sent_qty", 1)
        self.call(api.waiter_append_items, order_name=order.name,
                  items=json.dumps([{"item": self.cola, "qty": 2}]))
        order.reload()
        self.assertEqual(len(order.items), 2)
        fired = {i.item: int(i.sent_qty or 0) for i in order.items}
        self.assertEqual(fired[self.burger], 1, "already cooked")
        self.assertEqual(fired[self.cola], 0, "not yet fired — this is what reprints")

    def test_the_total_grows_with_the_appended_lines(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self.call(api.waiter_append_items, order_name=order.name,
                  items=json.dumps([{"item": self.cola, "qty": 1}]))
        order.reload()
        self.assertEqual(order.grand_total, 1375, "1250 of food plus 10% service")

    def test_the_waiter_order_list_names_the_creator_and_each_appender(self):
        order = self.order([{"item": self.burger, "qty": 1}], order_source="Waiter")
        second = "lf-test-second-waiter@example.com"
        if not frappe.db.exists("User", second):
            frappe.get_doc({
                "doctype": "User", "email": second, "first_name": "Second Waiter",
                "send_welcome_email": 0, "roles": [{"role": roles.WAITER}],
            }).insert(ignore_permissions=True)
        restaurant = order.get(branch.FIELD)
        if restaurant and not frappe.db.exists(
                "User Permission", {"user": second, "allow": "Restaurant",
                                    "for_value": restaurant}):
            permission = frappe.get_doc({
                "doctype": "User Permission", "user": second,
                "allow": "Restaurant", "for_value": restaurant,
                "apply_to_all_doctypes": 1,
            }).insert(ignore_permissions=True)
            self.addCleanup(
                lambda: frappe.db.exists("User Permission", permission.name) and
                frappe.delete_doc("User Permission", permission.name, force=True,
                                  ignore_permissions=True)
            )

        previous = frappe.session.user
        frappe.set_user(second)
        try:
            self.call(api.waiter_append_items, order_name=order.name,
                      items=json.dumps([{"item": self.cola, "qty": 1}]))
        finally:
            frappe.set_user(previous)

        result = self.call(api.get_my_orders)
        shown = next(o for o in result["orders"] if o.name == order.name)
        self.assertEqual(shown.staff_log[0]["action"], "created")
        self.assertEqual(shown.staff_log[0]["user"], order.owner)
        appenders = [e["user"] for e in shown.staff_log if e["action"] == "added"]
        self.assertIn(second, appenders)

    def test_it_refuses_an_order_that_does_not_exist(self):
        with self.assertRaises(Exception):
            self.call(api.waiter_append_items, order_name="_LFT-NOPE",
                      items=json.dumps([{"item": self.cola, "qty": 1}]))

    def test_a_second_waiter_ticket_on_the_table_appends_to_the_open_order(self):
        restaurant = branch.viewing()
        if not restaurant:
            self.skipTest("no active Restaurant")
        table = "_LFT-ORD-MERGE-" + frappe.generate_hash(length=6)
        row = {
            "doctype": "Restaurant Table",
            "__newname": table,
            "restaurant": restaurant,
            "no_of_seats": 4,
        }
        if frappe.db.has_column("Restaurant Table", "enabled"):
            row["enabled"] = 1
        table = frappe.get_doc(row).insert(
            ignore_permissions=True, ignore_mandatory=True
        ).name
        self._tables = [table]

        first = self.order(
            [{"item": self.burger, "qty": 1}],
            table=table,
            order_source="Waiter",
        )
        frappe.db.set_value("POS Order Item", first.items[0].name, "sent_qty", 1)
        second = self.call(
            api.send_to_kitchen,
            items=json.dumps([{"item": self.cola, "qty": 1}]),
            table=table,
            order_source="Waiter",
        )

        self.assertEqual(second["name"], first.name)
        self.assertTrue(second["appended"])
        first.reload()
        self.assertEqual([i.item for i in first.items], [self.burger, self.cola])


class TestOrderEndpointsRefuseOutsiders(OrderTestCase):
    """The Phase 1 guards, exercised as a real user rather than read from source."""

    def _as(self, role):
        email = f"lf-test-{role.lower().replace(' ', '-')}@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Test",
                            "send_welcome_email": 0,
                            "roles": [{"role": role}]}).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def test_a_guest_cannot_rewrite_an_order(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self._as(roles.KITCHEN_STAFF)  # stands in for "not POS staff"
        with self.assertRaises(frappe.PermissionError):
            self.call(api.update_order, order_name=order.name,
                      items=json.dumps([{"item": self.burger, "qty": 99}]))

    def test_a_waiter_cannot_remove_or_rebuild_existing_items(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self._as(roles.WAITER)
        with self.assertRaises(frappe.PermissionError):
            self.call(api.update_order, order_name=order.name,
                      items=json.dumps([{"item": self.cola, "qty": 1}]))
        order.reload()
        self.assertEqual([i.item for i in order.items], [self.burger])

    def test_kitchen_staff_cannot_read_the_edit_history(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        self._as(roles.KITCHEN_STAFF)
        with self.assertRaises(frappe.PermissionError):
            api.get_order_changelog(order.name)

    def test_a_guest_sees_a_receipt_not_the_kitchen_counters(self):
        order = self.order([{"item": self.burger, "qty": 2}])
        # The signature is what a guest has to hold at all now (S7b); what this
        # test is about is what they get once they do.
        sig = api.track_signature(order.name)
        previous = frappe.session.user
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, previous)
        line = api.get_order_items(order.name, s=sig)["items"][0]
        self.assertIn("qty", line)
        self.assertIn("rate", line)
        for staff_only in ("paid_qty", "sent_qty", "remaining", "notes"):
            self.assertNotIn(staff_only, line, f"{staff_only} is staff data")
