"""Report windows: the 7/30/90-day ranges, and picking a calendar month.

Ported from `main`, where the Manager app grew a month picker. The window used
to be "the last N days ending today" and nothing else, so a question as ordinary
as "what did April cost us" could not be asked.

Two things here are worth more than the feature. `_report_range` is one
resolver for all three reports, so the sales tab, the purchases tab and the food
cost report cannot disagree about what "this month" means. And the food-cost
window is now **closed at both ends** — it used to be open (`>= start` only), so
every total quietly included anything dated in the future, and a chosen month
would have run to the end of time.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, today

from luuvcrm import api, roles


class ReportWindowTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        email = "lf-test-report-manager@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Report Test",
                            "send_welcome_email": 0,
                            "roles": [{"role": roles.RESTAURANT_MANAGER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)


class TestTheWindowResolver(ReportWindowTestCase):
    def test_a_day_count_ends_today(self):
        start, end, days = api._report_range(days=7)
        self.assertEqual(days, 7)
        self.assertEqual(end, getdate(today()))
        self.assertEqual(start, getdate(add_days(today(), -6)))

    def test_nonsense_falls_back_and_the_window_is_capped(self):
        """A report is polled from a phone; a bad parameter must produce a
        smaller answer, never a table scan of the whole history."""
        self.assertEqual(api._report_range(days="junk")[2], 7)
        self.assertEqual(api._report_range(days=0)[2], 1)
        self.assertEqual(api._report_range(days=9999)[2], 92)
        self.assertEqual(api._report_range(days=9999, cap=365)[2], 365)

    def test_an_explicit_month_beats_the_day_count(self):
        start, end, days = api._report_range(days=7, start="2026-04-01", end="2026-04-30")
        self.assertEqual((str(start), str(end)), ("2026-04-01", "2026-04-30"))
        self.assertEqual(days, 30)

    def test_reversed_dates_are_swapped_rather_than_refused(self):
        """A date picker is how they get reversed, and an empty report is a
        worse answer than the obvious one."""
        start, end, _ = api._report_range(start="2026-04-30", end="2026-04-01")
        self.assertEqual((str(start), str(end)), ("2026-04-01", "2026-04-30"))

    def test_a_long_window_is_capped_from_the_end_that_matters(self):
        """Capping keeps the *recent* end: somebody asking for six years wants
        the latest data, not 2020."""
        start, end, days = api._report_range(start="2020-01-01", end="2026-04-30")
        self.assertEqual(days, 92)
        self.assertEqual(str(end), "2026-04-30")
        self.assertEqual(str(start), str(add_days(getdate("2026-04-30"), -91)))


class TestTheReportsHonourAMonth(ReportWindowTestCase):
    MONTH = {"start": "2026-04-01", "end": "2026-04-30"}

    def test_the_sales_report_does(self):
        m = api.get_manager_sales_report(days=7, **self.MONTH)
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(len(m["daily"]), 30)
        self.assertEqual(m["daily"][0]["date"], "2026-04-01")

    def test_the_purchase_report_does(self):
        m = api.get_manager_purchase_report(days=7, **self.MONTH)
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(len(m["daily"]), 30)

    def test_the_food_cost_report_does(self):
        m = api.get_food_cost_report(days=7, **self.MONTH)
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(m["days"], 30)

    def test_all_three_agree_on_what_a_window_is(self):
        """One resolver, so the sales tab, the purchases tab and the food cost
        report cannot disagree about what "this month" means."""
        windows = {
            (api.get_manager_sales_report(**self.MONTH)["start"],
             api.get_manager_sales_report(**self.MONTH)["end"]),
            (api.get_manager_purchase_report(**self.MONTH)["start"],
             api.get_manager_purchase_report(**self.MONTH)["end"]),
            (api.get_food_cost_report(**self.MONTH)["start"],
             api.get_food_cost_report(**self.MONTH)["end"]),
        }
        self.assertEqual(len(windows), 1)


class TestTheFoodCostWindowIsClosedAtBothEnds(ReportWindowTestCase):
    """It was open — `requested_at >= start` and nothing else — so a bill dated
    in the future counted towards this week's food cost, and a chosen month
    would have run to the end of time."""

    def test_a_bill_dated_after_the_window_is_not_counted(self):
        import json

        if not frappe.db.exists("DocType", "Purchase Bill"):
            self.skipTest("Purchase Bill is not installed")
        item = frappe.db.get_value("Item", {"disabled": 0}, "name")
        doc = frappe.get_doc({
            "doctype": "Purchase Bill", "vendor": "_LFT-Future-Vendor",
            "status": "approved", "requested_by": frappe.session.user,
            "requested_at": add_days(today(), 30), "total": 5000,
            "items": [{"item": item, "item_name": item, "qty": 1, "price": 5000}],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Purchase Bill", doc.name,
                        force=True, ignore_permissions=True)

        report = api.get_food_cost_report(days=7)
        self.assertNotIn(5000, (report["purchases"],),
                         "a bill dated a month from now is not this week's food cost")

    def test_it_is_still_a_number_when_nothing_happened(self):
        report = api.get_food_cost_report(start="2019-01-01", end="2019-01-31")
        self.assertEqual(report["purchases"], 0)
        self.assertEqual(report["revenue"], 0)
        self.assertEqual(report["profit"], 0)


class TestWhoMayReadThem(FrappeTestCase):
    def test_a_cashier_is_refused(self):
        email = "lf-test-report-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Report Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        for call in (api.get_manager_sales_report, api.get_manager_purchase_report,
                     api.get_food_cost_report):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.PermissionError):
                    call(days=7)
