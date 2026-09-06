"""Report windows: the 7/30/90-day ranges and the explicit month filter.

The manager Reports and Purchases tabs can now ask for a calendar month
(start+end) instead of only "the last N days"; these guard both shapes.
"""

import frappe
from frappe.utils import add_days, getdate, today
from frappe.tests.utils import FrappeTestCase
from luuvcrm import api
from luuvcrm.tests.utils import as_pos_user, ensure_setup, make_item, make_purchase_bill


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


class TestPurchaseDaySummary(FrappeTestCase):
    """The day card above the Bills list: today's bills split by where they stand,
    plus who entered them and who approved them."""

    def setUp(self):
        as_pos_user()
        ensure_setup()
        self.item = make_item()

    def _bill(self, qty, price, status="pending", by="Guest", resolved_by=None):
        b = make_purchase_bill([{"item": self.item, "qty": qty, "price": price}],
                               requested_by=by, status=status)
        if resolved_by:
            frappe.db.set_value("Purchase Bill", b.name, "resolved_by", resolved_by)
        return b

    def test_splits_the_day_by_status_and_names_the_people(self):
        # the site may already hold bills entered today, so assert on the delta
        base = api.get_purchase_day_summary()
        self.assertTrue(base["is_today"])
        by_user = lambda m, k: {p["user"]: p for p in m[k]}
        base_entered, base_appr = by_user(base, "entered_by"), by_user(base, "approved_by")

        self._bill(2, 100, "approved", by="Guest", resolved_by="Administrator")
        self._bill(1, 50, "approved", by="Administrator", resolved_by="Administrator")
        self._bill(3, 10, "pending", by="Guest")
        self._bill(1, 999, "rejected", by="Guest", resolved_by="Administrator")

        m = api.get_purchase_day_summary()
        self.assertEqual(m["count"] - base["count"], 4)
        self.assertEqual(len(m["bills"]), m["count"])
        self.assertEqual(m["total"] - base["total"], 200 + 50 + 30 + 999)
        for k, cnt, amt in (("approved", 2, 250), ("pending", 1, 30), ("rejected", 1, 999)):
            self.assertEqual(m[k]["count"] - base[k]["count"], cnt, k)
            self.assertEqual(round(m[k]["total"] - base[k]["total"], 2), amt, k)

        entered, appr = by_user(m, "entered_by"), by_user(m, "approved_by")
        self.assertEqual(entered["Guest"]["count"] - base_entered.get("Guest", {}).get("count", 0), 3)
        self.assertEqual(entered["Administrator"]["count"]
                         - base_entered.get("Administrator", {}).get("count", 0), 1)
        # sorted by spend, biggest first
        self.assertEqual([p["total"] for p in m["entered_by"]],
                         sorted([p["total"] for p in m["entered_by"]], reverse=True))
        # only approved bills count towards the approver — the rejected one does not
        self.assertEqual(appr["Administrator"]["count"]
                         - base_appr.get("Administrator", {}).get("count", 0), 2)
        self.assertEqual(round(appr["Administrator"]["total"]
                               - base_appr.get("Administrator", {}).get("total", 0), 2), 250)

        # the card lists the day's bills, newest first, with the lines the sheet needs
        mine = [b for b in m["bills"] if b["vendor"] == "Test Vendor"]
        self.assertEqual(len(mine), 4)
        self.assertTrue(all(b["lines"] for b in mine))
        self.assertEqual({b["status"] for b in mine}, {"approved", "pending", "rejected"})

    def test_todays_bill_does_not_show_up_on_another_day(self):
        prev = add_days(today(), -1)
        base = api.get_purchase_day_summary(date=prev)
        self.assertFalse(base["is_today"])
        self._bill(2, 100, "approved")
        m = api.get_purchase_day_summary(date=prev)
        self.assertEqual(m["count"], base["count"])
        self.assertEqual(m["total"], base["total"])
