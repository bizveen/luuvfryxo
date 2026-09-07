"""The trail where the money is.

`SELF_LOGGED` named 21 endpoints that the generic `after_request` layer skips
"because they log themselves with richer detail". **They did not.** There was
not one `log_activity()` call outside `activity.py`, on this branch or on main,
and `git log -S` finds none that was ever removed. So the trail covered the
reads and the small stuff and stopped exactly where the money is, and the flag
detector — double-tap, big discount, cash variance, reprint, duplicate approval
— had nothing to detect, because every rule reads `amount` / `total_before` /
`total_after`, which only the semantic layer writes.

`test_activity` asserted the arrangement ("the money-moving endpoints write a
richer row themselves, so the request hook must stay out of their way"), which
is how it stayed invisible for so long: the test said the layers agree, and both
layers were silent.

These tests run the real endpoints and read the table back, because the failure
that matters is not an exception — it is a row that is never written.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import activity, api, roles
from luuvcrm.tests import fixtures as fx


class TrailTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        activity.setup_activity_log()
        self.burger = fx.item(self, "TRAIL-BURGER")
        fx.item_price(self, self.burger, 1000)
        fx.service_charge(self, 0)
        self._orders = []
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        for name in reversed(self._orders):
            if not frappe.db.exists("POS Order", name):
                continue
            try:
                doc = frappe.get_doc("POS Order", name)
                if doc.docstatus == 1:
                    doc.db_set("docstatus", 0)
                frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
            except Exception:
                frappe.db.rollback()

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def order(self, items=None, **kwargs):
        items = items or [{"item": self.burger, "qty": 1}]
        result = self.call(api.send_to_kitchen, items=json.dumps(items), **kwargs)
        self._orders.append(result["name"])
        return frappe.get_doc("POS Order", result["name"])

    def rows(self, action=None, ref_name=None):
        """The trail, newest first. Read with SQL rather than get_all because
        these rows are written with a raw INSERT and are not committed — a
        get_list under a different user would not see them either way."""
        where, args = ["1=1"], []
        if action:
            where.append("action = %s")
            args.append(action)
        if ref_name:
            where.append("ref_name = %s")
            args.append(ref_name)
        return frappe.db.sql(
            "select * from `tab{0}` where {1} order by creation desc".format(
                activity.DOCTYPE, " and ".join(where)), tuple(args), as_dict=True)

    def one(self, action, ref_name=None):
        found = self.rows(action, ref_name)
        self.assertTrue(found, f"nothing was written for {action}")
        return found[0]


class TestEverySelfLoggedEndpointActuallyLogs(FrappeTestCase):
    """The list said 21 endpoints log themselves. This is the check that they do
    — by reading the source, so it covers the ones with no fixture cheap enough
    to drive here (a shift close needs a whole till). A method on the list with
    no call in its body is an endpoint the generic layer skips and nothing
    replaces: a hole with a label on it."""

    def _module_source(self):
        import pathlib

        api_dir = pathlib.Path(frappe.get_app_path("luuvcrm", "api"))
        return {p.stem: p.read_text() for p in api_dir.glob("*.py")}

    def test_no_endpoint_is_on_the_list_without_writing_a_row(self):
        import re

        sources = self._module_source()
        missing = []
        for method in sorted(activity.SELF_LOGGED):
            body = None
            for text in sources.values():
                match = re.search(r"^def %s\(.*?(?=\n@frappe|\nclass |\Z)" % method,
                                  text, re.S | re.M)
                if match:
                    body = match.group(0)
                    break
            if body is None:
                missing.append(f"{method}: not found in luuvcrm/api/")
            elif "log_activity(" not in body:
                missing.append(f"{method}: no log_activity() call")
        self.assertEqual(missing, [],
                         "SELF_LOGGED promises the generic layer stays away from "
                         f"these; they write nothing: {missing}")


class TestTheSaleIsRecorded(TrailTestCase):
    def test_firing_a_ticket_writes_the_total(self):
        order = self.order()
        row = self.one("order.fire", order.name)
        self.assertEqual(float(row.total_after), float(order.grand_total))

    def test_paying_writes_the_amount_taken(self):
        order = self.order()
        self.call(api.process_payment, order_name=order.name, payment_mode="Cash")
        row = self.one("payment.process", order.name)
        self.assertEqual(float(row.amount), float(order.grand_total))
        self.assertEqual(row.source, "server")

    def test_the_row_names_the_cashier_not_the_request(self):
        order = self.order()
        self.call(api.process_payment, order_name=order.name, payment_mode="Cash")
        self.assertEqual(self.one("payment.process", order.name).user, frappe.session.user)

    def test_cancelling_records_what_the_bill_was_worth(self):
        order = self.order()
        self.call(api.cancel_order, order_name=order.name)
        row = self.one("order.cancel", order.name)
        self.assertEqual(float(row.total_before), float(order.grand_total))
        self.assertEqual(float(row.total_after), 0)

    def test_an_edit_carries_the_total_on_both_sides_of_it(self):
        """"The bill was Rs 2,000 more than it should be" is the question this
        table exists to answer, and it cannot be answered from a row that says
        only that an edit happened."""
        order = self.order()
        before = float(order.grand_total)
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 3}]))
        row = self.one("order.update", order.name)
        self.assertEqual(float(row.total_before), before)
        self.assertEqual(float(row.total_after), before * 3)

    def test_deleting_a_draft_leaves_the_only_record_there_is(self):
        order = self.order()
        name, total = order.name, float(order.grand_total)
        self.call(api.delete_draft_order, order_name=name)
        self.assertFalse(frappe.db.exists("POS Order", name))
        row = self.one("order.delete", name)
        self.assertEqual(float(row.total_before), total)

    def test_moving_a_bill_between_tables_says_where_it_went(self):
        order = self.order()
        table = frappe.db.get_value("Restaurant Table", {}, "name")
        if not table:
            self.skipTest("no Restaurant Table on this site")
        self.call(api.move_order_table, order_name=order.name, table=table)
        row = self.one("order.move", order.name)
        self.assertIn(table, row.detail or "")


class TestTheFlagsCanFireAtAll(TrailTestCase):
    """Each rule reads amount / total_before / total_after, so before the
    semantic layer existed none of them could ever be true. These prove the
    wiring, not the thresholds — those are `activity`'s own tests."""

    def test_a_big_discount_on_a_paid_bill_is_flagged(self):
        order = self.order([{"item": self.burger, "qty": 1}])
        order.db_set("discount_amount", float(order.grand_total) * 0.5)
        self.call(api.process_payment, order_name=order.name, payment_mode="Cash")
        row = self.one("payment.process", order.name)
        self.assertEqual(row.flag, 1)
        self.assertEqual(row.flag_reason, activity.FLAG_BIG_DISCOUNT)

    def test_an_ordinary_bill_is_not_flagged(self):
        order = self.order()
        self.call(api.process_payment, order_name=order.name, payment_mode="Cash")
        self.assertEqual(self.one("payment.process", order.name).flag, 0)

    def test_pulling_a_fired_item_off_the_bill_is_flagged(self):
        """The kitchen cooked it and it is no longer on the bill. Nothing else
        in the system notices."""
        order = self.order([{"item": self.burger, "qty": 2}])
        self.call(api.mark_kot_printed, order_name=order.name)
        self.call(api.update_order, order_name=order.name,
                  items=json.dumps([{"item": self.burger, "qty": 1}]))
        row = self.one("order.update", order.name)
        self.assertEqual(row.flag_reason, activity.FLAG_AFTER_KOT)

    def test_a_second_print_of_the_same_document_is_flagged(self):
        order = self.order()
        self.call(api.log_print, order_name=order.name, print_type="receipt")
        self.assertEqual(self.rows("print", order.name)[0].flag, 0)
        self.call(api.log_print, order_name=order.name, print_type="receipt")
        self.assertEqual(self.rows("print", order.name)[0].flag, 1,
                         "the reprint is the one worth a manager's eye")

    def test_asking_twice_for_the_same_approval_is_flagged(self):
        order = self.order()
        for _ in range(2):
            self.call(api.create_approval, approval_type="discount",
                      pos_order=order.name, title="Discount")
        rows = self.rows("approval.create", order.name)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].flag_reason, activity.FLAG_DUP_APPROVAL)
        self.assertEqual(rows[1].flag, 0, "the first request is not a duplicate")


class TestTheTrailRidesOnTheCallersTransaction(FrappeTestCase):
    """`log_activity` used to commit by default. A semantic call happens
    half-way through taking money, so that commit wrote out whatever the payment
    had done so far and a later throw would have left it there. It also
    committed this suite's own rollback away, which is how it was found: shifts
    opened by one test survived into the next.
    """

    def test_the_default_is_not_to_commit(self):
        import inspect

        signature = inspect.signature(activity.log_activity)
        self.assertIs(signature.parameters["commit"].default, False)

    def test_only_the_request_hook_asks_for_a_commit(self):
        import inspect

        # `_insert(row, commit=True)` keeps its own default: it is the private
        # writer, and the two callers that matter say what they want. What must
        # not come back is a caller inside an endpoint asking for a commit.
        self.assertIn("commit=True", inspect.getsource(activity.log_request_activity))
        self.assertNotIn("commit=True", inspect.getsource(activity.log_activity))
        self.assertIn("commit=False", inspect.getsource(activity.log_ui_activity))

    def test_a_logged_endpoint_writes_nothing_the_rollback_cannot_undo(self):
        before = frappe.db.sql("select count(*) from `tab%s`" % activity.DOCTYPE)[0][0]
        activity.log_activity(action="trail.rollback.test", amount=1)
        frappe.db.rollback()
        after = frappe.db.sql("select count(*) from `tab%s`" % activity.DOCTYPE)[0][0]
        self.assertEqual(after, before,
                         "a log row must not outlive the transaction it describes")
