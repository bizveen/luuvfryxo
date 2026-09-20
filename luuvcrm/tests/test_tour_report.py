"""What a tour company has brought in, and its statement.

Every figure is made by taking real money through the real endpoints and reading
the report back, because the failure that matters is a statement that adds up and
disagrees with the till — or with the rate card the company was quoted. The
companies are named `_LFT ...` and the report is filtered to them, so trade
already on the dev site cannot move a number.
"""

import json
from unittest import mock

import frappe
from frappe.utils import add_days, today

from luuvcrm import api, roles
from luuvcrm.api import tour_report
from luuvcrm.api.ratecard import card_rate
from luuvcrm.tests.test_tour_billing import TourBillingTestCase, NAME


class TourReportTestCase(TourBillingTestCase):
    def sale(self, items, company=NAME, day=None, **kw):
        """A paid bill under a company; `day` moves its posting date."""
        order = self.order(items)
        result = self.pay(order, **({"tour_company": company} if company else {}), **kw)
        if day:
            frappe.db.set_value("POS Invoice", result["invoice_name"], "posting_date", day)
        return result

    def report(self, company=NAME, start=None, end=None):
        return api.get_tour_report(start=start or add_days(today(), -30), end=end or today(),
                                   company=company)

    def block(self, company=NAME, **kw):
        found = [c for c in self.report(company, **kw)["companies"] if c["company"] == company]
        return found[0] if found else None


class TestTheSums(TourReportTestCase):
    def test_one_bill_at_the_rate_card_price(self):
        self.sale([{"item": self.burger, "qty": 2}])
        b = self.block()
        self.assertEqual((b["bills"], b["menu"], b["discounted"], b["commission"]), (1, 2000, 1800, 200))
        self.assertEqual(b["billed"], 2200, "the invoice total, service charge and all")
        self.assertEqual(b["discount_label"], "10%")

    def test_the_commission_agrees_with_the_rate_card_not_a_flat_percent(self):
        """250 less 10% is 225, and the card rounds that up to 230. A flat 10% would
        say 25 of commission; the company was quoted 230, so it is owed 20."""
        self.sale([{"item": self.cola, "qty": 1}])
        b = self.block()
        self.assertEqual(card_rate(250, 10), 230)
        self.assertEqual((b["discounted"], b["commission"]), (230, 20))

    def test_bills_add_up(self):
        self.sale([{"item": self.burger, "qty": 1}])
        self.sale([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 2}])
        b = self.block()
        self.assertEqual((b["bills"], b["menu"], b["commission"]), (2, 2500, 100 + 100 + 40))

    def test_the_parts_add_up_to_the_whole(self):
        self.sale([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 3}], day=add_days(today(), -1))
        self.sale([{"item": self.cola, "qty": 1}])
        b = self.block()
        for part in ("days", "items"):
            for key in ("menu", "discounted", "commission"):
                self.assertAlmostEqual(sum(x[key] for x in b[part]), b[key], 2, f"{part}.{key}")
        self.assertEqual(sum(d["bills"] for d in b["days"]), b["bills"])
        self.assertAlmostEqual(b["menu"] - b["discounted"], b["commission"], 2)

    def test_the_same_report_with_no_company_filter_carries_it_too(self):
        self.sale([{"item": self.burger, "qty": 1}])
        self.assertIn(NAME, [c["company"] for c in api.get_tour_report(
            start=add_days(today(), -30), end=today())["companies"]])


class TestWhichBillsCount(TourReportTestCase):
    def test_a_bill_under_no_company_is_not_counted(self):
        self.sale([{"item": self.burger, "qty": 1}], company=None)
        self.assertIsNone(self.block())

    def test_another_companys_bill_is_not_counted(self):
        self.company("_LFT Green Wave", 15)
        self.sale([{"item": self.burger, "qty": 1}], company="_LFT Green Wave")
        self.assertIsNone(self.block())
        self.assertEqual(self.block("_LFT Green Wave")["commission"], 150)

    def test_only_bills_in_the_dates_count(self):
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), -40))
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), 5))
        self.sale([{"item": self.burger, "qty": 1}])
        self.assertEqual(self.block()["bills"], 1)

    def test_the_first_and_last_day_are_inside(self):
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), -3))
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), -1))
        self.assertEqual(self.block(start=add_days(today(), -3), end=add_days(today(), -1))["bills"], 2)

    def test_a_split_bill_counts_only_its_own_lines(self):
        """The order has a burger and a cola; only the cola was billed to the company."""
        order = self.order([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 1}])
        self.split(order, [{"item": self.cola, "qty": 1}], tour_company=NAME)
        self.split(order, [{"item": self.burger, "qty": 1}])
        b = self.block()
        self.assertEqual([i["name"] for i in b["items"]], [frappe.db.get_value("Item", self.cola, "item_name")
                                                            or self.cola])
        self.assertEqual(b["menu"], 250)

    def test_no_bills_is_an_empty_report_not_an_error(self):
        self.assertIsNone(self.block(start=add_days(today(), -400), end=add_days(today(), -390)))
        self.assertEqual(api.get_tour_report(start="2001-01-01", end="2001-01-31")["totals"]["bills"], 0)


class TestTheDayByDay(TourReportTestCase):
    def test_bills_are_grouped_by_the_day_they_were_raised(self):
        d1, d2 = add_days(today(), -2), add_days(today(), -1)
        self.sale([{"item": self.burger, "qty": 1}], day=d1)
        self.sale([{"item": self.burger, "qty": 1}], day=d1)
        self.sale([{"item": self.cola, "qty": 1}], day=d2)
        days = self.block()["days"]
        self.assertEqual([(d["date"], d["bills"], d["menu"]) for d in days],
                         [(str(d1), 2, 2000), (str(d2), 1, 250)])

    def test_a_day_with_no_bills_is_not_a_row(self):
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), -5))
        self.sale([{"item": self.burger, "qty": 1}])
        self.assertEqual(len(self.block()["days"]), 2)

    def test_items_are_summed_across_bills_biggest_first(self):
        self.sale([{"item": self.burger, "qty": 1}, {"item": self.cola, "qty": 2}])
        self.sale([{"item": self.cola, "qty": 3}])
        items = self.block()["items"]
        self.assertEqual([(i["qty"], i["menu"]) for i in items], [(5, 1250), (1, 1000)])


class TestThePercentOnTheDay(TourReportTestCase):
    def test_a_later_change_of_discount_does_not_restate_an_earlier_sale(self):
        self.sale([{"item": self.burger, "qty": 1}])
        frappe.db.set_value("Tour Company", NAME, "discount_percent", 20)
        self.assertEqual(self.block()["commission"], 100)
        self.sale([{"item": self.burger, "qty": 1}])
        b = self.block()
        self.assertEqual(b["commission"], 100 + 200)
        self.assertEqual(b["percents"], [10.0, 20.0])
        self.assertEqual(b["discount_label"], "10% to 20%, as at each sale")


class TestTheStatement(TourReportTestCase):
    def html(self, **kw):
        api.print_tour_statement(company=NAME, start=add_days(today(), -30), end=today(), **kw)
        return frappe.local.response["filecontent"]

    def test_it_says_who_when_and_how_much(self):
        self.sale([{"item": self.burger, "qty": 2}])
        html = self.html()
        for text in ("Commission statement", NAME, "Day by day", "Items", "Guests paid the full menu price",
                     "2,000.00", "1,800.00", "200.00", "Commission (10%)"):
            self.assertIn(text, html)
        self.assertIn(str(today()), html)

    def test_each_day_has_a_row_and_the_total_closes_the_table(self):
        self.sale([{"item": self.burger, "qty": 1}], day=add_days(today(), -1))
        self.sale([{"item": self.burger, "qty": 1}])
        html = self.html()
        self.assertIn(str(add_days(today(), -1)), html)
        self.assertEqual(html.count('<tr class="total">'), 2)     # days and items

    def test_a_company_with_no_bills_gets_a_statement_that_says_so(self):
        self.assertIn("No bills were booked", self.html())

    def test_the_screen_version_has_the_buttons_and_the_printed_one_does_not(self):
        self.sale([{"item": self.burger, "qty": 1}])
        screen = self.html()
        self.assertIn("window.print()", screen)
        self.assertIn("print_tour_statement?", screen)
        self.assertIn("format=pdf", screen)
        c = self.block()
        printed = tour_report.statement_html(c, add_days(today(), -30), today())
        self.assertNotIn("window.print()", printed)

    def test_the_pdf_is_the_printed_page(self):
        self.sale([{"item": self.burger, "qty": 1}])
        with mock.patch("frappe.utils.pdf.get_pdf", return_value=b"%PDF-x") as pdf:
            self.html(format="pdf")
        self.assertIn("Commission statement", pdf.call_args[0][0])
        self.assertNotIn("Download PDF", pdf.call_args[0][0])
        self.assertTrue(frappe.local.response["filename"].endswith(".pdf"))

    def test_no_pdf_engine_is_an_explained_error(self):
        with mock.patch("frappe.utils.pdf.get_pdf", side_effect=OSError("no wkhtmltopdf")):
            with self.assertRaises(frappe.ValidationError):
                self.html(format="pdf")

    def test_nothing_that_is_typed_can_break_the_page(self):
        """Frappe will not name a record with a `<`, but an item's name is free text
        and it reaches the page all the same."""
        c = {"company": "Acme", "bills": 1, "billed": 1.0, "menu": 1.0, "discounted": 1.0, "commission": 0.0,
             "percents": [10.0], "discount_label": "10%", "days": [],
             "items": [{"name": "<script>x</script>", "qty": 1, "menu": 1.0, "discounted": 1.0, "commission": 0.0}]}
        html = tour_report.statement_html(c, add_days(today(), -1), today())
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_file_is_named_for_the_company_and_the_dates(self):
        self.html()
        self.assertEqual(frappe.local.response["filename"],
                         f"statement-lft-blue-wave-{add_days(today(), -30)}-to-{today()}.html")

    def test_an_unknown_company_is_refused(self):
        for name in ("", "_LFT Nobody"):
            with self.assertRaises(frappe.DoesNotExistError):
                api.print_tour_statement(company=name)


class TestWho(TourReportTestCase):
    def test_a_cashier_may_read_neither(self):
        email = "lf-test-tourrep-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Tour Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        for call in (api.get_tour_report, api.print_tour_statement):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.PermissionError):
                    call()

    def test_the_filter_offers_every_company_removed_ones_too(self):
        self.company("_LFT Gone4", 10, disabled=1)
        names = api.get_tour_report()["names"]
        self.assertIn(NAME, names)
        self.assertIn("_LFT Gone4", names)


class TestTheDefaultWindow(TourReportTestCase):
    def test_no_dates_means_this_month_so_far(self):
        r = api.get_tour_report()
        self.assertEqual(r["end"], str(today()))
        self.assertEqual(r["start"], str(today())[:8] + "01")

    def test_a_reversed_range_is_put_right(self):
        r = api.get_tour_report(start=today(), end=add_days(today(), -3))
        self.assertEqual((r["start"], r["end"]), (str(add_days(today(), -3)), str(today())))


class TestTheManagerPage(TourBillingTestCase):
    """Strings in a page nobody re-reads: a renamed endpoint is a dead button on the
    manager's phone, not an error."""

    def setUp(self):
        super().setUp()
        self.html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "manager.html"))

    def test_the_report_is_not_a_tab_of_its_own(self):
        """One link, one tab: the sales open from the company's row."""
        self.assertNotIn('id="tabTourReport"', self.html)
        self.assertNotIn("moreGo('tourreport')", self.html)
        self.assertEqual(self.html.count("moreGo('ratecard')"), 1)
        self.assertIn("Tour companies</span>", self.html)

    def test_a_company_row_opens_its_sales_and_back_returns_to_the_list(self):
        for text in ('id="tcList"', 'id="tcSales"', "onclick=\"tcOpen(${i})\"", 'onclick="tcBack()"',
                     "function tcOpen(", "function tcBack("):
            self.assertIn(text, self.html)

    def test_it_has_a_date_range_that_reloads_the_report_and_no_company_picker(self):
        for field in ('id="trStart"', 'id="trEnd"'):
            self.assertIn(field, self.html)
        self.assertEqual(self.html.count('onchange="loadTourReport(true)"'), 2)
        self.assertNotIn('id="trCompany"', self.html, "the company is the one that was tapped")

    def test_it_asks_for_the_report_of_the_company_that_was_opened(self):
        self.assertIn("rcCall('get_tour_report',{start:start,end:end,company:company})", self.html)
        self.assertIn("company=asked", self.html)
        self.assertIn("/api/method/luuvcrm.api.print_tour_statement?", self.html)
        for name in ("get_tour_report", "print_tour_statement"):
            frappe.is_whitelisted(frappe.get_attr(f"luuvcrm.api.{name}"))

    def test_a_slow_answer_for_the_last_company_cannot_paint_over_the_next(self):
        self.assertIn("if(asked!==tcCompany) return;", self.html)

    def test_the_statement_button_is_on_every_company_card(self):
        self.assertIn("Statement · Print / PDF", self.html)
        self.assertIn("tourStatement(", self.html)

    def test_the_boxes_offer_what_the_report_sums(self):
        for text in ("Commission owed", "Sales at menu price", "Discounted price", "Day by day", "Items taken"):
            self.assertIn(text, self.html)
