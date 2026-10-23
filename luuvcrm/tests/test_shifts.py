"""Opening and closing a till.

A shift is the frame every sale hangs on: ERPNext will not raise a POS Invoice
without an open POS Opening Entry, and the day's takings are whatever the
closing entry gathered. Two failures here are silent and expensive — an opening
left in draft (which happened once already and was fixed), and a close that
misses invoices, which shows the cashier a smaller number than the drawer holds.

These run as their own cashier rather than as Administrator, because a shift is
per-user and the site's other open shifts must not leak into the assertions.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.tests import fixtures as fx

CASHIER_EMAIL = "lf-test-shift-cashier@example.com"


class ShiftTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        roles.ensure_pos_docperms()
        self.profile = frappe.db.get_value("POS Profile", {}, "name")
        self.cashier = self._cashier()
        self._as_cashier()
        self.addCleanup(self._close_any_open_shift)

    def _cashier(self):
        if not frappe.db.exists("User", CASHIER_EMAIL):
            frappe.get_doc({
                "doctype": "User", "email": CASHIER_EMAIL, "first_name": "Shift",
                "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}],
            }).insert(ignore_permissions=True)
        return CASHIER_EMAIL

    def _as_cashier(self):
        previous = frappe.session.user
        frappe.set_user(self.cashier)
        self.addCleanup(frappe.set_user, previous)

    def _close_any_open_shift(self):
        """Leave no shift behind — an open one would fail the next run's
        'a second shift is refused' assertion for the wrong reason."""
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in frappe.get_all("POS Opening Entry",
                                       filters={"user": self.cashier}, pluck="name"):
                try:
                    doc = frappe.get_doc("POS Opening Entry", name)
                    doc.flags.ignore_permissions = True
                    if doc.docstatus == 1:
                        doc.db_set("status", "Closed")
                        doc.cancel()
                    frappe.delete_doc("POS Opening Entry", name, force=True,
                                      ignore_permissions=True)
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

    def open_shift(self, **kwargs):
        kwargs.setdefault("pos_profile", self.profile)
        self.call(api.pos_open_shift, **kwargs)
        return frappe.get_doc("POS Opening Entry", frappe.db.get_value(
            "POS Opening Entry", {"user": self.cashier, "status": "Open", "docstatus": 1},
            "name", order_by="creation desc"))


class TestOpeningAShift(ShiftTestCase):
    def test_the_opening_entry_is_submitted_not_left_in_draft(self):
        """This was a real bug: pos_open_shift inserted without submitting, so
        every opening sat at docstatus 0. ERPNext only recognises a submitted
        one, so the shift did not really exist."""
        opening = self.open_shift(opening_balance=5000)
        self.assertEqual(opening.docstatus, 1)
        self.assertEqual(opening.status, "Open")

    def test_it_belongs_to_the_cashier_who_opened_it(self):
        opening = self.open_shift(opening_balance=0)
        self.assertEqual(opening.user, self.cashier)

    def test_the_counted_float_is_recorded_against_cash(self):
        opening = self.open_shift(opening_balance=5000)
        amounts = {d.mode_of_payment: d.opening_amount for d in opening.balance_details}
        self.assertEqual(amounts.get("Cash"), 5000)

    def test_every_payment_mode_on_the_profile_gets_a_row(self):
        """The close reconciles mode by mode, so a mode missing here has nothing
        to reconcile against."""
        opening = self.open_shift(opening_balance=1000)
        expected = set(api._profile_payment_modes(self.profile))
        self.assertEqual({d.mode_of_payment for d in opening.balance_details}, expected)

    def test_per_mode_opening_amounts_are_honoured(self):
        opening = self.open_shift(balance_details=json.dumps([
            {"mode_of_payment": "Cash", "opening_amount": 2500},
        ]))
        amounts = {d.mode_of_payment: float(d.opening_amount) for d in opening.balance_details}
        self.assertEqual(amounts["Cash"], 2500)

    def test_a_malformed_balance_payload_falls_back_rather_than_failing(self):
        """A shift that cannot be opened stops the restaurant trading, so bad
        JSON degrades to zero floats instead of throwing."""
        opening = self.open_shift(balance_details="{not json")
        self.assertTrue(opening.balance_details)
        self.assertEqual(sum(float(d.opening_amount or 0) for d in opening.balance_details), 0)

    def test_a_second_shift_is_refused(self):
        self.open_shift(opening_balance=1000)
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.pos_open_shift, pos_profile=self.profile, opening_balance=1000)
        self.assertIn("already open", str(caught.exception))

    def test_a_cashier_may_open_one_without_borrowed_permissions(self):
        """pos_open_shift inserts under the caller's own permissions. Before the
        grants moved into a patch, a site where nobody had called
        setup_pos_shift_perms had a till its cashiers could not open."""
        self.assertTrue(frappe.has_permission("POS Opening Entry", "create", user=self.cashier))
        self.assertTrue(frappe.has_permission("POS Opening Entry", "submit", user=self.cashier))
        self.open_shift(opening_balance=0)

    def test_someone_who_is_not_pos_staff_is_refused(self):
        email = "lf-test-shift-kitchen@example.com"
        if not frappe.db.exists("User", email):
            frappe.set_user("Administrator")
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Kitchen",
                            "send_welcome_email": 0,
                            "roles": [{"role": roles.KITCHEN_STAFF}]}).insert(ignore_permissions=True)
        frappe.set_user(email)
        with self.assertRaises(frappe.PermissionError):
            self.call(api.pos_open_shift, pos_profile=self.profile, opening_balance=0)


class TestReadingTheShift(ShiftTestCase):
    def test_there_is_no_shift_before_one_is_opened(self):
        self.assertIsNone(self.call(api.get_pos_shift)["shift"])

    def test_it_reports_the_open_shift(self):
        self.open_shift(opening_balance=3000)
        shift = self.call(api.get_pos_shift)["shift"]
        self.assertIsNotNone(shift)
        self.assertEqual(shift["opening_balance"], 3000)
        self.assertEqual(shift["cashier"], "Shift",
                         "the screen shows the cashier's name, not their login")

    def test_ensure_open_shift_opens_one_when_there_is_none(self):
        """A sale must never fail because nobody pressed Open Shift."""
        name = api._ensure_open_shift(self.profile)
        self.assertTrue(name)
        self.assertEqual(frappe.db.get_value("POS Opening Entry", name, "status"), "Open")

    def test_ensure_open_shift_reuses_the_one_already_open(self):
        opening = self.open_shift(opening_balance=1000)
        self.assertEqual(api._ensure_open_shift(self.profile), opening.name,
                         "a second opening would split the day's takings in two")


class TestClosingAShift(ShiftTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.burger = fx.item(self, "SHIFT-BURGER")
        fx.item_price(self, self.burger, 1000)
        fx.service_charge(self, 0)  # keep the arithmetic about the drawer
        frappe.set_user(self.cashier)
        self._orders = []
        self.addCleanup(self._drop_orders)

    def _drop_orders(self):
        """Committed, because closing a shift commits.

        `pos_close_shift` commits by design — the consolidation behind it cannot
        run inside the caller's transaction — so every order a closing test made
        is already durable and the framework's rollback will not take it back
        out. A cleanup that does not commit deletes nothing: that is how this
        site collected ~300 draft POS Orders and crossed the kitchen screen's
        200-ticket ceiling, which then failed a test that had nothing to do
        with shifts.
        """
        user = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for name in self._orders:
                if frappe.db.exists("POS Order", name):
                    try:
                        doc = frappe.get_doc("POS Order", name)
                        if doc.docstatus == 1:
                            doc.db_set("docstatus", 0)
                        frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True)
                    except Exception:
                        frappe.db.rollback()
            frappe.db.commit()
        finally:
            frappe.set_user(user)

    def sell(self, qty=1, **kwargs):
        result = self.call(api.send_to_kitchen,
                           items=json.dumps([{"item": self.burger, "qty": qty}]),
                           order_type="Dine In")
        name = result["name"] if isinstance(result, dict) else result
        self._orders.append(name)
        paid = self.call(api.process_payment, order_name=name, **kwargs)
        fx.drop_invoice(self, paid["invoice_name"])
        return paid

    def test_closing_without_an_open_shift_is_refused(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self.call(api.pos_close_shift, closing_balance=0)
        self.assertIn("No open shift", str(caught.exception))

    def test_the_shift_closes_and_the_opening_is_marked_closed(self):
        opening = self.open_shift(opening_balance=1000)
        self.sell()
        self.call(api.pos_close_shift, closing_balance=2000)
        opening.reload()
        self.assertEqual(opening.status, "Closed")
        self.assertTrue(opening.pos_closing_entry)

    def test_the_day_s_sales_reach_the_closing_entry(self):
        self.open_shift(opening_balance=1000)
        self.sell(qty=2)
        shift = self.call(api.pos_close_shift, closing_balance=3000)["shift"]
        self.assertEqual(shift["total_sales"], 2000)
        self.assertEqual(shift["order_count"], 1)

    def test_a_short_drawer_is_reported_as_a_difference(self):
        """The point of counting: the closing entry must say what is missing,
        not quietly accept the expected figure."""
        opening = self.open_shift(opening_balance=1000)
        self.sell()
        self.call(api.pos_close_shift, closing_balance=1900)  # 100 short
        opening.reload()
        closing = frappe.get_doc("POS Closing Entry", opening.pos_closing_entry)
        cash = [r for r in closing.payment_reconciliation if r.mode_of_payment == "Cash"][0]
        self.assertEqual(float(cash.closing_amount), 1900)
        self.assertEqual(float(cash.difference), -100)

    def test_an_uncounted_mode_is_trusted_at_its_expected_amount(self):
        opening = self.open_shift(opening_balance=1000)
        self.sell()
        self.call(api.pos_close_shift)  # nothing counted
        opening.reload()
        closing = frappe.get_doc("POS Closing Entry", opening.pos_closing_entry)
        for row in closing.payment_reconciliation:
            self.assertEqual(float(row.difference), 0)

    def test_a_third_mode_gets_its_own_row_on_the_z_report(self):
        """The Z-report prints one "<mode> sales" line per reconciliation row.
        A mode with no row prints nothing, and the shift tallies without it."""
        frappe.set_user("Administrator")
        mode = fx.delivery_payment_mode(self, "PickMe")
        frappe.set_user(self.cashier)

        opening = self.open_shift(opening_balance=1000)
        self.sell(payment_mode=mode)
        shift = self.call(api.pos_close_shift, closing_balance=1000)["shift"]

        self.assertEqual(shift["payment_breakdown"].get(mode), 1000)
        opening.reload()
        closing = frappe.get_doc("POS Closing Entry", opening.pos_closing_entry)
        rows = {r.mode_of_payment: r for r in closing.payment_reconciliation}
        self.assertIn(mode, rows)
        self.assertEqual(float(rows[mode].expected_amount), 1000)

    def test_it_is_trusted_rather_than_counted(self):
        """Only cash has a drawer. The aggregator's total is known exactly, so
        it auto-fills and the close cannot report it short."""
        frappe.set_user("Administrator")
        mode = fx.delivery_payment_mode(self, "PickMe")
        frappe.set_user(self.cashier)

        opening = self.open_shift(opening_balance=1000)
        self.sell(payment_mode=mode)
        self.call(api.pos_close_shift, closing_balance=1000)   # drawer untouched
        opening.reload()
        closing = frappe.get_doc("POS Closing Entry", opening.pos_closing_entry)
        rows = {r.mode_of_payment: r for r in closing.payment_reconciliation}
        self.assertEqual(float(rows[mode].difference), 0)
        self.assertEqual(float(rows["Cash"].closing_amount), 1000)
        self.assertEqual(float(rows["Cash"].difference), 0)

    def test_the_true_shift_start_is_not_overwritten(self):
        """The close widens period_start_date in memory to catch a sale paid
        just before the shift opened, then must put the real one back — the
        opening is saved afterwards."""
        opening = self.open_shift(opening_balance=1000)
        started = opening.period_start_date
        self.sell()
        self.call(api.pos_close_shift, closing_balance=2000)
        opening.reload()
        self.assertEqual(opening.period_start_date, started)

    def test_a_shift_with_no_sales_still_closes(self):
        opening = self.open_shift(opening_balance=1000)
        shift = self.call(api.pos_close_shift, closing_balance=1000)["shift"]
        opening.reload()
        self.assertEqual(opening.status, "Closed")
        self.assertEqual(shift["total_sales"], 0)
        self.assertEqual(shift["status"], "closed")
