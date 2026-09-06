"""Report windows: the 7/30/90-day ranges and the explicit month filter.

The manager Reports and Purchases tabs can now ask for a calendar month
(start+end) instead of only "the last N days"; these guard both shapes.
"""

import frappe
from frappe.utils import add_days, getdate, today
from frappe.tests.utils import FrappeTestCase
from luuvcrm import api
from luuvcrm.tests.utils import as_pos_user, ensure_setup


class TestReportRange(FrappeTestCase):
    def setUp(self):
        as_pos_user()
        ensure_setup()

    def test_days_window_ends_today(self):
        start, end, days = api._report_range(days=7)
        self.assertEqual(days, 7)
        self.assertEqual(end, getdate(today()))
        self.assertEqual(start, getdate(add_days(today(), -6)))

    def test_days_falls_back_and_is_capped(self):
        self.assertEqual(api._report_range(days="junk")[2], 7)
        self.assertEqual(api._report_range(days=0)[2], 1)
        self.assertEqual(api._report_range(days=9999)[2], 92)
        self.assertEqual(api._report_range(days=9999, cap=365)[2], 365)

    def test_explicit_month_wins_over_days(self):
        start, end, days = api._report_range(days=7, start="2026-04-01", end="2026-04-30")
        self.assertEqual(str(start), "2026-04-01")
        self.assertEqual(str(end), "2026-04-30")
        self.assertEqual(days, 30)

    def test_reversed_dates_are_swapped_and_long_windows_capped(self):
        start, end, _ = api._report_range(start="2026-04-30", end="2026-04-01")
        self.assertEqual((str(start), str(end)), ("2026-04-01", "2026-04-30"))
        start, end, days = api._report_range(start="2020-01-01", end="2026-04-30")
        self.assertEqual(days, 92)
        self.assertEqual(str(end), "2026-04-30")

    def test_sales_report_honours_a_month(self):
        m = api.get_manager_sales_report(days=7, start="2026-04-01", end="2026-04-30")
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(len(m["daily"]), 30)
        self.assertEqual(m["daily"][0]["date"], "2026-04-01")

    def test_purchase_report_honours_a_month(self):
        m = api.get_manager_purchase_report(days=7, start="2026-04-01", end="2026-04-30")
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(len(m["daily"]), 30)

    def test_food_cost_report_honours_a_month(self):
        m = api.get_food_cost_report(days=7, start="2026-04-01", end="2026-04-30")
        self.assertEqual((m["start"], m["end"]), ("2026-04-01", "2026-04-30"))
        self.assertEqual(m["days"], 30)
