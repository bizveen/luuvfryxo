"""The profit-and-loss statement.

The rule these tests hold to: every rupee that was spent or taken appears on
exactly one line, and the lines add up to the totals. A statement whose lines
disagree with its total is worse than none, because it gets handed on.

Expenses and bills are dated in 2019 so the figures are the test's own; the
sales are taken through the real till endpoints and read back as a change on
today, the way the dashboard tests do.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.api import pnl
from luuvcrm.api.expenses import EXPENSE_CATEGORIES
from luuvcrm.tests import fixtures as fx
from luuvcrm.tests.test_dashboard import DashboardTestCase

MONTH = {"start": "2019-03-01", "end": "2019-03-31"}


def _line(statement, section, label):
    return next(x["amount"] for x in statement[section] if x["label"] == label)


class PnlTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        if not frappe.db.exists("DocType", "Expense"):
            self.skipTest("no Expense on this site")

    def expense(self, category, amount, status="approved", day="2019-03-10"):
        if not frappe.db.exists("Expense Category", category):
            frappe.get_doc({"doctype": "Expense Category", "category_name": category}
                           ).insert(ignore_permissions=True)
        doc = frappe.get_doc({"doctype": "Expense", "category": category, "payee": "_LFT",
                              "amount": amount, "expense_date": day, "status": status})
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        self.addCleanup(frappe.delete_doc, "Expense", doc.name, force=True, ignore_permissions=True)

    def bill(self, total, status="approved", day="2019-03-10"):
        if not frappe.db.exists("DocType", "Purchase Bill"):
            self.skipTest("no Purchase Bill on this site")
        doc = frappe.get_doc({"doctype": "Purchase Bill", "vendor": "_LFT-PNL-Vendor",
                              "status": status, "purchase_date": day, "total": total})
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        self.addCleanup(frappe.delete_doc, "Purchase Bill", doc.name,
                        force=True, ignore_permissions=True)

    def report(self, **kw):
        return api.get_manager_pnl(**(kw or MONTH))


class TestTheCostLines(PnlTestCase):
    def test_each_expense_lands_on_its_own_line(self):
        self.expense("Rent", 1000)
        self.expense("Salaries/Wages", 2000)
        self.expense("Water", 30)
        self.expense("Gas (LP)", 400)
        self.expense("Charities", 5)
        r = self.report()
        self.assertEqual(_line(r, "costs", "Rent"), 1000)
        self.assertEqual(_line(r, "costs", "Payroll"), 2000)
        self.assertEqual(_line(r, "costs", "Water bill"), 30)
        self.assertEqual(_line(r, "costs", "Utilities (gas)"), 400)
        self.assertEqual(_line(r, "costs", "Charities"), 5)
        self.assertEqual(_line(r, "costs", "Electricity bill"), 0)

    def test_the_lines_are_the_ones_on_the_template_in_its_order(self):
        labels = [c["label"] for c in self.report()["costs"]]
        self.assertEqual(labels[:3], ["Rent", "Payroll", "Advertising"])
        self.assertEqual(labels[-2:], ["Food cost", "Other cost"])
        self.assertEqual(len(labels), 19)

    def test_packaging_has_its_own_line(self):
        self.expense("Packaging", 850)
        r = self.report()
        self.assertEqual(_line(r, "costs", "Packaging"), 850)
        self.assertEqual(_line(r, "costs", "Other cost"), 0)

    def test_only_approved_expenses_count(self):
        self.expense("Rent", 1000, status="pending")
        self.expense("Rent", 700, status="rejected")
        self.expense("Rent", 100)
        self.assertEqual(_line(self.report(), "costs", "Rent"), 100)

    def test_outside_the_window_is_not_counted(self):
        self.expense("Rent", 1000, day="2019-02-28")
        self.expense("Rent", 1000, day="2019-04-01")
        self.assertEqual(_line(self.report(), "costs", "Rent"), 0)

    def test_a_category_with_no_line_is_other_cost_not_lost(self):
        self.expense("Cleaning & Consumables", 50)
        self.expense("_LFT Brand New Category", 25)
        r = self.report()
        self.assertEqual(_line(r, "costs", "Other cost"), 75)
        self.assertEqual(r["total_cost"], 75)

    def test_every_category_a_site_seeds_reaches_a_line(self):
        """Add a category to the seed and forget the statement, and its money
        would still be in Other cost — but nobody would be told. Say so."""
        mapped = {c for _, cats in pnl.COST_LINES for c in cats}
        strays = set(EXPENSE_CATEGORIES) - mapped - {api.FOOD_PURCHASE_CATEGORY}
        self.assertEqual(strays, {"Cleaning & Consumables", "Licences/Fees", "Other"},
                         "a seeded category has no line on the statement")

    def test_the_total_is_the_sum_of_the_lines(self):
        self.expense("Rent", 1000)
        self.expense("Taxes", 10.5)
        self.bill(300)
        r = self.report()
        self.assertEqual(r["total_cost"], round(sum(c["amount"] for c in r["costs"]), 2))
        self.assertEqual(r["total_cost"], 1310.5)


class TestFoodCostIsApprovedBillsOnly(PnlTestCase):
    def test_approved_bills_count_pending_and_rejected_do_not(self):
        self.bill(500)
        self.bill(9000, status="pending")
        self.bill(9000, status="rejected")
        self.assertEqual(_line(self.report(), "costs", "Food cost"), 500)

    def test_wastage_is_not_added(self):
        if not frappe.db.exists("DocType", "Wastage"):
            self.skipTest("no Wastage on this site")
        self.bill(500)
        doc = frappe.get_doc({"doctype": "Wastage", "cost": 77})
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.set_value("Wastage", doc.name, "creation", "2019-03-10 12:00:00",
                            update_modified=False)
        self.addCleanup(frappe.delete_doc, "Wastage", doc.name, force=True, ignore_permissions=True)
        self.assertEqual(_line(self.report(), "costs", "Food cost"), 500)

    def test_a_bill_counts_on_the_day_it_was_bought(self):
        self.bill(500, day="2019-02-28")
        self.assertEqual(_line(self.report(), "costs", "Food cost"), 0)

    def test_the_statement_says_what_food_cost_is(self):
        self.assertIn("purchase bills", self.report()["food_cost_basis"])


class TestTheBottomLine(PnlTestCase):
    def test_a_month_with_costs_and_no_sales_is_a_loss_with_no_percentage(self):
        self.expense("Rent", 1000)
        r = self.report()
        self.assertEqual(r["total_sales"], 0)
        self.assertEqual(r["profit"], -1000)
        self.assertIsNone(r["profit_pct"])

    def test_the_html_says_loss_and_the_amount_unsigned(self):
        self.expense("Rent", 1000)
        html = pnl.pnl_html(self.report())
        self.assertIn("LOSS", html)
        self.assertIn("1,000.00", html)
        self.assertNotIn("-1,000.00", html)


class TestTheFixedExpenseNote(PnlTestCase):
    def missing(self, **kw):
        return {m["label"]: m["pending"] for m in self.report(**kw)["missing_fixed"]}

    def test_a_month_with_no_rent_or_payroll_is_flagged(self):
        self.assertEqual(self.missing(), {"Rent": False, "Payroll": False})

    def test_an_entered_one_is_not(self):
        self.expense("Rent", 1000)
        self.assertEqual(self.missing(), {"Payroll": False})
        self.expense("Salaries/Wages", 2000)
        self.assertEqual(self.missing(), {})

    def test_one_waiting_for_approval_says_so_rather_than_asking_again(self):
        self.expense("Rent", 1000, status="pending")
        self.assertEqual(self.missing(), {"Rent": True, "Payroll": False})

    def test_a_short_window_is_not_nagged(self):
        """Rent is paid once a month; a week that does not contain it is normal."""
        self.assertEqual(api.get_manager_pnl(days=7)["missing_fixed"], [])

    def test_the_note_is_on_the_screen_and_not_on_the_printed_sheet(self):
        d = self.report()
        self.assertIn("No approved Rent, Payroll", pnl.pnl_html(d, toolbar=True))
        self.assertNotIn("No approved", pnl.pnl_html(d))
        self.assertNotIn("Print / Save as PDF", pnl.pnl_html(d))


class TestFoodAndBeveragesFromKitchenGroups(DashboardTestCase):
    def setUp(self):
        super().setUp()
        group = frappe.db.get_value("Kitchen Group", {"group_name": "Beverages"}, "name")
        if not group or not frappe.db.has_column("Item", "kitchen_group"):
            self.skipTest("no Beverages Kitchen Group on this site")
        frappe.db.set_value("Item", self.cola, "kitchen_group", group)
        self.pnl_before = api.get_manager_pnl(days=1)

    def moved(self, section, label):
        return round(_line(api.get_manager_pnl(days=1), section, label)
                     - _line(self.pnl_before, section, label), 2)

    def test_a_drink_is_beverages_and_a_dish_is_food(self):
        self.sell([{"item": self.burger, "qty": 2}, {"item": self.cola, "qty": 1}],
                  payment_mode="Cash")
        self.assertEqual(self.moved("income", "Food sales"), 2000)
        self.assertEqual(self.moved("income", "Beverages sales"), 250)

    def test_the_lines_add_up_to_total_sales_which_is_total_revenue(self):
        self.sell([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 2}],
                  payment_mode="Cash")
        r = api.get_manager_pnl(days=1)
        self.assertEqual(r["total_sales"], round(sum(i["amount"] for i in r["income"]), 2))
        self.assertEqual(r["total_sales"], api.get_manager_dashboard()["revenue"])
        self.assertEqual(round(r["total_sales"] - self.pnl_before["total_sales"], 2), 1500)

    def test_an_item_with_no_kitchen_group_is_food(self):
        frappe.db.set_value("Item", self.cola, "kitchen_group", None)
        self.sell([{"item": self.cola, "qty": 1}], payment_mode="Cash")
        self.assertEqual(self.moved("income", "Food sales"), 250)
        self.assertEqual(self.moved("income", "Beverages sales"), 0)


class TestThePrintedPage(PnlTestCase):
    def test_it_carries_every_line_and_the_period(self):
        html = pnl.pnl_html(self.report())
        for text in ("Profit and loss statement", "March 2019", "Cost and expenses", "Rent",
                     "Municipal council", "Food cost", "TOTAL COST", "Income and sales",
                     "Food sales", "Beverages sales", "Other income", "TOTAL SALES"):
            self.assertIn(text, html)

    def test_a_typed_payee_or_brand_cannot_break_the_page(self):
        d = self.report()
        d["brand"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>alert(1)", pnl.pnl_html(d))

    def test_the_screen_version_offers_print_and_pdf(self):
        html = pnl.pnl_html(self.report(), toolbar=True, pdf_url="/x?format=pdf")
        self.assertIn("window.print()", html)
        self.assertIn('href="/x?format=pdf"', html)

    def test_the_endpoint_hands_back_the_page(self):
        api.print_manager_pnl(**MONTH)
        r = frappe.local.response
        self.assertEqual(r["content_type"], "text/html")
        self.assertIn("Profit and loss statement", r["filecontent"])
        self.assertIn("format=pdf", r["filecontent"])
        self.assertEqual(r["filename"], "profit-and-loss-2019-03-01-to-2019-03-31.html")

    def test_the_pdf_is_a_pdf_when_the_server_can_make_one(self):
        import shutil
        if not shutil.which("wkhtmltopdf"):
            self.skipTest("wkhtmltopdf is not installed here")
        api.print_manager_pnl(format="pdf", **MONTH)
        self.assertTrue(frappe.local.response["filecontent"].startswith(b"%PDF"))
        self.assertTrue(frappe.local.response["filename"].endswith(".pdf"))

    def test_no_pdf_engine_is_an_explained_error_not_a_crash(self):
        from unittest import mock
        with mock.patch("frappe.utils.pdf.get_pdf", side_effect=OSError("no wkhtmltopdf")):
            with self.assertRaises(frappe.ValidationError):
                api.print_manager_pnl(format="pdf", **MONTH)


class TestWhoMayReadIt(FrappeTestCase):
    def test_a_cashier_is_refused_both(self):
        email = "lf-test-pnl-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Pnl Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        for call in (api.get_manager_pnl, api.print_manager_pnl):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.PermissionError):
                    call(days=30)


class TestTheManagerPage(FrappeTestCase):
    """The button is a string in a page nobody re-reads — a renamed endpoint
    fails as a dead button on the manager's phone, not as an error."""

    def page(self, name):
        return frappe.read_file(frappe.get_app_path("luuvcrm", "www", name))

    def test_the_reports_tab_has_the_button_and_it_points_at_a_real_endpoint(self):
        html = self.page("manager.html")
        self.assertIn('id="pnlBtn"', html)
        self.assertIn("/api/method/luuvcrm.api.print_manager_pnl?", html)
        self.assertIn("openPnl()", html)
        fn = frappe.get_attr("luuvcrm.api.print_manager_pnl")
        self.assertTrue(frappe.is_whitelisted(fn) is None)  # raises if it is not

    def test_the_button_sends_the_range_the_tab_is_on(self):
        self.assertIn("new URLSearchParams(repArgs())", self.page("manager.html"))

    def test_the_worker_does_not_cache_the_statement_as_the_app(self):
        """A navigation to the printed page went through the worker, which filed
        the response under '/manager' — the app's offline fallback."""
        body = "\n".join(l.split("//")[0] for l in self.page("manager_sw.js").splitlines())
        self.assertIn("pathname !== '/manager'", body)


class TestTheCardAndTheStatementAgree(PnlTestCase):
    """Sales vs costs used to add wastage to food cost and the statement did not,
    so two screens gave two profits for one month."""

    def test_wastage_is_reported_but_not_added_to_food_cost(self):
        if not frappe.db.exists("DocType", "Wastage"):
            self.skipTest("no Wastage on this site")
        self.bill(500)
        doc = frappe.get_doc({"doctype": "Wastage", "cost": 77})
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        frappe.db.set_value("Wastage", doc.name, "creation", "2019-03-10 12:00:00",
                            update_modified=False)
        self.addCleanup(frappe.delete_doc, "Wastage", doc.name, force=True, ignore_permissions=True)
        card = api.get_food_cost_report(**MONTH)
        self.assertEqual(card["food_cost"], 500)
        self.assertEqual(card["wastage"], 77)

    def test_the_profit_is_the_same_on_both(self):
        self.bill(500)
        self.expense("Rent", 1000)
        self.expense("Other", 40)
        card = api.get_food_cost_report(**MONTH)
        statement = self.report()
        self.assertEqual(card["profit"], statement["profit"])
        self.assertEqual(card["food_cost"], _line(statement, "costs", "Food cost"))
        self.assertEqual(card["overhead"], statement["total_cost"] - card["food_cost"])
