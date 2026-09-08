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
from luuvcrm.tests import fixtures as fx


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


class TestADiscountReachesThePurchaseReport(FrappeTestCase):
    """A discount is entered at approval and written to the bill's `total`, not
    to any line. A report that sums the lines reports what was asked for rather
    than what was paid — and disagrees with Sales vs costs, which reads
    `total`."""

    AMOUNT, DISCOUNT = 10000.0, 1500.0

    def setUp(self):
        super().setUp()
        if not frappe.db.exists("DocType", "Purchase Bill"):
            self.skipTest("no Purchase Bill on this site")
        self.item = fx.item(self, "PURCH-DISC-RICE")
        self.bill = self._bill()

    def _bill(self):
        doc = frappe.get_doc({
            "doctype": "Purchase Bill",
            "vendor": "LF Test Vendor",
            "status": "approved",
            "requested_at": frappe.utils.now_datetime(),
            "total": self.AMOUNT,
            "items": [{"item": self.item, "item_name": "Rice", "uom": "Kg",
                       "qty": 100, "price": 100}],
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        frappe.db.commit()
        self.addCleanup(self._drop, doc.name)
        return doc

    def _drop(self, name):
        if frappe.db.exists("Purchase Bill", name):
            frappe.delete_doc("Purchase Bill", name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def discount(self, amount):
        self.bill.db_set("discount", amount)
        self.bill.db_set("total", self.AMOUNT - amount)
        frappe.db.commit()

    def report(self):
        return api.get_manager_purchase_report(days=1)

    def spend_for(self, rows, key, name):
        return next((r["spend"] for r in rows if r[key] == name), 0)

    def test_the_day_s_spend_is_what_was_paid(self):
        before = self.report()["total_spend"]
        self.discount(self.DISCOUNT)
        self.assertAlmostEqual(before - self.report()["total_spend"], self.DISCOUNT, 2)

    def test_the_vendor_is_credited_with_the_discount_too(self):
        before = self.spend_for(self.report()["vendors"], "name", "LF Test Vendor")
        self.discount(self.DISCOUNT)
        after = self.spend_for(self.report()["vendors"], "name", "LF Test Vendor")
        self.assertAlmostEqual(before - after, self.DISCOUNT, 2)

    def test_the_item_list_carries_its_share(self):
        """The discount is the bill's, not the line's — pro-rated by value, or
        the item list stops adding up to the same money as every other card."""
        self.discount(self.DISCOUNT)
        self.assertAlmostEqual(self.spend_for(self.report()["items"], "name", "Rice"),
                               self.AMOUNT - self.DISCOUNT, 2)

    def test_every_breakdown_agrees_with_the_total(self):
        self.discount(self.DISCOUNT)
        report = self.report()
        for key in ("vendors", "items"):
            self.assertAlmostEqual(sum(r["spend"] for r in report[key]),
                                   report["total_spend"], 2, key)

    def test_a_bill_with_no_discount_is_unchanged(self):
        report = self.report()
        self.assertAlmostEqual(self.spend_for(report["items"], "name", "Rice"),
                               self.AMOUNT, 2)


class TestWhatThePurchaseReportNowSays(FrappeTestCase):
    """Six readings off data the bills already carried and nothing showed."""

    def setUp(self):
        super().setUp()
        if not frappe.db.exists("DocType", "Purchase Bill"):
            self.skipTest("no Purchase Bill on this site")
        self.rice = fx.item(self, "PURCH-CMP-RICE")
        self._bills = []
        self.addCleanup(self._drop)

    def _drop(self):
        for name in self._bills:
            if frappe.db.exists("Purchase Bill", name):
                frappe.delete_doc("Purchase Bill", name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def bill(self, vendor, price, qty=10, status="approved", old_price=0, when=None):
        doc = frappe.get_doc({
            "doctype": "Purchase Bill", "vendor": vendor, "status": status,
            "requested_at": when or frappe.utils.now_datetime(),
            "total": qty * price,
            "items": [{"item": self.rice, "item_name": "Cmp Rice", "uom": "Kg",
                       "qty": qty, "price": price, "old_price": old_price}],
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        frappe.db.commit()
        self._bills.append(doc.name)
        return doc

    def report(self, days=1):
        return api.get_manager_purchase_report(days=days)

    def row(self, rows, name="Cmp Rice"):
        return next((r for r in rows if r["name"] == name), None)

    # ── same item, different vendors ──
    def test_a_price_gap_between_vendors_is_reported(self):
        self.bill("Cheap Vendor", 100)
        self.bill("Dear Vendor", 150)
        row = self.row(self.report()["price_spread"])
        self.assertEqual((row["low"], row["high"], row["vendors"]), (100, 150, 2))
        self.assertEqual(row["spread_pct"], 50.0)
        self.assertEqual(row["could_save"], 50 * 20)   # the gap over both bills' qty

    def test_one_vendor_is_not_a_comparison(self):
        self.bill("Only Vendor", 100)
        self.bill("Only Vendor", 150)
        self.assertIsNone(self.row(self.report()["price_spread"]))

    def test_two_units_are_not_two_quotes_for_the_same_thing(self):
        """Rs 100/Kg against Rs 100/Nos is not a spread; grouping them would
        invent one."""
        self.bill("Cheap Vendor", 100)
        b = self.bill("Dear Vendor", 150)
        frappe.db.set_value("Purchase Bill Item", b.items[0].name, "uom", "Nos")
        frappe.db.commit()
        self.assertIsNone(self.row(self.report()["price_spread"]))

    # ── what changed price ──
    def test_a_rise_is_reported_against_the_old_price(self):
        self.bill("V", 120, old_price=100)
        row = self.row(self.report()["price_moves"])
        self.assertEqual((row["was"], row["now"], row["change_pct"]), (100, 120, 20.0))

    def test_a_price_that_did_not_move_is_not_news(self):
        self.bill("V", 100, old_price=100)
        self.assertIsNone(self.row(self.report()["price_moves"]))

    # ── owed to vendors ──
    def test_an_unpaid_bill_is_owed(self):
        self.bill("Slow Payer", 100)
        row = self.row(self.report()["owed"], "Slow Payer")
        self.assertEqual(row["amount"], 1000)
        self.assertEqual(row["count"], 1)

    def test_a_paid_bill_is_not(self):
        b = self.bill("Prompt Payer", 100)
        b.db_set("payment_status", "paid")
        frappe.db.commit()
        self.assertIsNone(self.row(self.report()["owed"], "Prompt Payer"))

    def test_what_is_owed_ignores_the_window(self):
        """A bill from March is owed today whatever range the screen is on."""
        self.bill("Old Debt", 100, when=frappe.utils.add_days(frappe.utils.now_datetime(), -200))
        self.assertIsNotNone(self.row(self.report(days=1)["owed"], "Old Debt"))

    # ── pending and rejected ──
    def test_pending_spend_is_counted_apart_from_approved(self):
        before = self.report()["waiting"]["pending"]["amount"]
        self.bill("V", 100, status="pending")
        report = self.report()
        self.assertEqual(report["waiting"]["pending"]["amount"] - before, 1000)

    def test_a_pending_bill_stays_out_of_the_spend(self):
        before = self.report()["total_spend"]
        self.bill("V", 100, status="pending")
        self.assertEqual(self.report()["total_spend"], before)

    # ── spend against sales ──
    def test_spend_is_shown_against_the_period_s_revenue(self):
        report = self.report()
        if report["revenue"] > 0:
            self.assertAlmostEqual(report["spend_pct"],
                                   round(report["total_spend"] * 100.0 / report["revenue"], 1), 1)
        else:
            self.assertEqual(report["spend_pct"], 0)


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
