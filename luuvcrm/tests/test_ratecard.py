"""The tour-company rate card.

The figures are what a client is quoted, so the tests pin the arithmetic (the
rounding has a floating-point trap in it) and the rules a card must not break:
no price above the menu's, no item the manager unticked, no menu price shown.
The menu itself is stubbed — this is about the card, not the till.
"""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.api import ratecard

MENU = [
    {"code": "K1", "name": "Chicken Kottu", "rate": 1000, "group": "Kottu"},
    {"code": "K2", "name": "Cheese Kottu", "rate": 1250, "group": "Kottu"},
    {"code": "B1", "name": "Iced Coffee", "rate": 455, "group": "Beverages"},
    {"code": "X0", "name": "Unpriced Thing", "rate": 0, "group": "Kottu"},
    {"code": "S1", "name": "Rice <b>&</b> Curry", "rate": 850, "group": "Rice"},
]


class RateCardTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("luuvcrm.api.ratecard.menu.menu_items", return_value=[dict(e) for e in MENU])
        p.start()
        self.addCleanup(p.stop)

    def html(self, company="Acme Tours", discount=10, items=("K1", "K2", "B1", "S1"), **kw):
        api.print_ratecard(company=company, discount=discount, items=json.dumps(list(items)), **kw)
        return frappe.local.response["filecontent"]


class TestThePrice(FrappeTestCase):
    def test_ten_percent_off_a_round_price_is_exact_not_rounded_up_a_step(self):
        """1000 * 0.9 is 900.0000000000001; rounding that up quotes 910."""
        self.assertEqual(ratecard.card_rate(1000, 10), 900)

    def test_it_rounds_up_to_the_next_ten(self):
        self.assertEqual(ratecard.card_rate(1250, 10), 1130)   # 1125
        self.assertEqual(ratecard.card_rate(455, 10), 410)     # 409.5

    def test_it_never_quotes_above_the_menu_price(self):
        self.assertEqual(ratecard.card_rate(455, 0), 455)      # not 460
        self.assertEqual(ratecard.card_rate(1250, 0), 1250)
        self.assertEqual(ratecard.card_rate(455, 0.1), 455)    # 454.5 rounds up to 460: capped

    def test_no_discount_on_a_round_price_is_the_menu_price(self):
        self.assertEqual(ratecard.card_rate(850, 0), 850)

    def test_the_most_discount_still_rounds_sensibly(self):
        self.assertEqual(ratecard.card_rate(1000, 50), 500)
        self.assertEqual(ratecard.card_rate(333, 50), 170)     # 166.5


class TestWhatIsOnTheCard(RateCardTestCase):
    def test_only_the_ticked_items_are_on_it(self):
        html = self.html(items=("K1",))
        self.assertIn("Chicken Kottu", html)
        for gone in ("Cheese Kottu", "Iced Coffee"):
            self.assertNotIn(gone, html)

    def test_the_prices_are_the_discounted_ones_and_the_menu_price_is_not_shown(self):
        html = self.html(discount=10, items=("K1", "K2"))
        self.assertIn(">900<", html)
        self.assertIn(">1,130<", html)
        self.assertNotIn("1,000", html)
        self.assertNotIn("1,250", html)
        self.assertNotIn(">1000<", html)

    def test_an_item_with_no_price_is_never_offered_or_printed(self):
        self.assertNotIn("X0", [i["code"] for g in api.get_ratecard_menu()["groups"] for i in g["items"]])
        self.assertNotIn("Unpriced Thing", self.html(items=("K1", "X0")))

    def test_a_code_that_is_not_on_the_menu_is_ignored(self):
        html = self.html(items=("K1", "NOPE"))
        self.assertIn("Chicken Kottu", html)

    def test_items_are_grouped_under_their_headings(self):
        html = self.html()
        for heading in ("Kottu", "Beverages", "Rice"):
            self.assertIn(f"<h3>{heading}</h3>", html)

    def test_the_form_is_offered_the_menu_grouped_and_priced(self):
        got = api.get_ratecard_menu()
        self.assertEqual([g["group"] for g in got["groups"]], ["Kottu", "Beverages", "Rice"])
        self.assertEqual(got["count"], 4)
        self.assertEqual(got["groups"][0]["items"][0], {"code": "K1", "name": "Chicken Kottu", "rate": 1000.0})


class TestTheHeaderAndFooter(RateCardTestCase):
    def test_the_company_is_named_at_the_top(self):
        html = self.html(company="Blue Wave Transport")
        self.assertIn("<h1>Special Rates for Blue Wave Transport</h1>", html)
        self.assertLess(html.index("Special Rates for"), html.index("Chicken Kottu"))

    def test_the_footer_carries_the_service_charge_note(self):
        self.assertIn("+ 10% Service Charge will be added", self.html())

    def test_nothing_typed_can_break_the_page(self):
        html = self.html(company="<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("Rice &lt;b&gt;&amp;&lt;/b&gt; Curry", self.html(items=("S1",)))

    def test_the_screen_version_has_the_buttons_and_the_printed_one_does_not(self):
        screen = self.html()
        self.assertIn("window.print()", screen)
        self.assertIn("Download PDF", screen)
        self.assertIn('name="csrf_token"', screen)
        printed = ratecard.ratecard_html("Acme", 10, MENU[:1])
        self.assertNotIn("Download PDF", printed)
        self.assertNotIn("window.print()", printed)

    def test_the_download_button_sends_back_exactly_what_made_the_card(self):
        screen = self.html(company="Acme Tours", discount=15, items=("K1", "B1"))
        self.assertIn('name="company" value="Acme Tours"', screen)
        self.assertIn('name="discount" value="15.0"', screen)
        self.assertIn("&quot;K1&quot;", screen)


class TestWhatIsRefused(RateCardTestCase):
    def refuses(self, **kw):
        args = dict(company="Acme", discount=10, items=json.dumps(["K1"]))
        args.update(kw)
        with self.assertRaises(frappe.ValidationError):
            api.print_ratecard(**args)

    def test_no_company_name(self):
        self.refuses(company="   ")

    def test_a_discount_outside_zero_to_fifty(self):
        self.refuses(discount=-1)
        self.refuses(discount=50.5)
        self.refuses(discount=100)
        self.refuses(discount="ten")

    def test_the_limits_themselves_are_fine(self):
        self.html(discount=0)
        self.html(discount=50)

    def test_nothing_ticked(self):
        self.refuses(items=json.dumps([]))
        self.refuses(items=json.dumps(["NOPE"]))
        self.refuses(items="not json")

    def test_a_cashier_may_not_make_one(self):
        email = "lf-test-ratecard-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Card Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        for call in (api.get_ratecard_menu, api.print_ratecard, api.get_tour_companies,
                     api.get_tour_company, api.save_tour_company, api.archive_tour_company):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.PermissionError):
                    call()


class TestTheDownload(RateCardTestCase):
    def test_the_page_is_named_for_the_company(self):
        self.html(company="Blue Wave Transport!")
        self.assertEqual(frappe.local.response["filename"], "rate-card-blue-wave-transport.html")

    def test_the_pdf_is_a_pdf_when_the_server_can_make_one(self):
        import shutil
        if not shutil.which("wkhtmltopdf"):
            self.skipTest("wkhtmltopdf is not installed here")
        self.html(format="pdf")
        self.assertTrue(frappe.local.response["filecontent"].startswith(b"%PDF"))

    def test_no_pdf_engine_is_an_explained_error(self):
        with mock.patch("frappe.utils.pdf.get_pdf", side_effect=OSError("no wkhtmltopdf")):
            with self.assertRaises(frappe.ValidationError):
                self.html(format="pdf")

    def test_the_pdf_is_made_from_the_printed_page_not_the_screen_one(self):
        with mock.patch("frappe.utils.pdf.get_pdf", return_value=b"%PDF-x") as pdf:
            self.html(format="pdf")
        made_from = pdf.call_args[0][0]
        self.assertNotIn("Download PDF", made_from)
        self.assertIn("Special Rates for Acme Tours", made_from)


class SavedCompanyTestCase(RateCardTestCase):
    def save(self, company="_LFT Blue Wave", discount=10, items=("K1", "K2"), **kw):
        out = api.save_tour_company(company=company, discount=discount,
                                    items=json.dumps(list(items)), **kw)
        self.addCleanup(lambda: [frappe.delete_doc("Tour Company", n, force=True, ignore_permissions=True)
                                 for n in (out["name"], company) if frappe.db.exists("Tour Company", n)])
        return out

    def listed(self):
        return {c["name"]: c for c in api.get_tour_companies()["companies"]}


class TestSavingACompany(SavedCompanyTestCase):
    def test_a_saved_company_is_listed_with_its_discount_and_items(self):
        self.save(discount=12.5, items=("K1", "K2", "B1"))
        c = self.listed()["_LFT Blue Wave"]
        self.assertEqual((c["discount"], c["items"], c["on_menu"]), (12.5, 3, 3))

    def test_it_can_be_read_back_for_editing(self):
        self.save(items=("K1", "B1"))
        self.assertEqual(api.get_tour_company(name="_LFT Blue Wave"),
                         {"name": "_LFT Blue Wave", "discount": 10.0, "items": ["K1", "B1"]})

    def test_an_item_that_is_not_on_the_menu_is_not_kept(self):
        self.save(items=("K1", "NOPE", "X0"))          # X0 has no price
        self.assertEqual(api.get_tour_company(name="_LFT Blue Wave")["items"], ["K1"])

    def test_the_same_name_cannot_be_saved_twice(self):
        self.save()
        with self.assertRaises(frappe.ValidationError):
            self.save()

    def test_bad_input_is_refused_and_nothing_is_saved(self):
        for kw in ({"discount": 51}, {"discount": -1}, {"items": ()}, {"items": ("NOPE",)},
                   {"company": "  "}):
            with self.subTest(kw=kw):
                with self.assertRaises(frappe.ValidationError):
                    self.save(**{"company": "_LFT Bad", **kw})
        self.assertFalse(frappe.db.exists("Tour Company", "_LFT Bad"))

    def test_a_company_can_be_updated(self):
        self.save(discount=10, items=("K1",))
        self.save(discount=20, items=("K1", "B1"), original="_LFT Blue Wave")
        self.assertEqual(self.listed()["_LFT Blue Wave"]["discount"], 20)
        self.assertEqual(api.get_tour_company(name="_LFT Blue Wave")["items"], ["K1", "B1"])
        self.assertEqual(len([n for n in self.listed() if n.startswith("_LFT")]), 1)

    def test_a_changed_name_is_a_rename_not_a_second_company(self):
        self.save()
        out = self.save(company="_LFT Green Wave", original="_LFT Blue Wave")
        self.assertEqual(out["name"], "_LFT Green Wave")
        self.assertFalse(frappe.db.exists("Tour Company", "_LFT Blue Wave"))
        self.assertEqual(frappe.db.get_value("Tour Company", "_LFT Green Wave", "company_name"),
                         "_LFT Green Wave")

    def test_a_rename_onto_another_company_is_refused(self):
        self.save("_LFT One")
        self.save("_LFT Two")
        with self.assertRaises(frappe.ValidationError):
            self.save("_LFT Two", original="_LFT One")
        self.assertTrue(frappe.db.exists("Tour Company", "_LFT One"))

    def test_a_change_of_case_alone_keeps_the_name(self):
        self.save("_LFT Blue Wave")
        out = self.save("_LFT BLUE WAVE", discount=15, original="_LFT Blue Wave")
        self.assertEqual(out["name"], "_LFT Blue Wave")
        self.assertEqual(self.listed()["_LFT Blue Wave"]["discount"], 15)


class TestRemovingACompany(SavedCompanyTestCase):
    def test_it_leaves_the_list_but_the_record_stays(self):
        self.save()
        api.archive_tour_company(name="_LFT Blue Wave")
        self.assertNotIn("_LFT Blue Wave", self.listed())
        self.assertEqual(frappe.db.get_value("Tour Company", "_LFT Blue Wave", "disabled"), 1)

    def test_a_removed_company_cannot_be_printed_or_opened(self):
        self.save()
        api.archive_tour_company(name="_LFT Blue Wave")
        for call in (api.print_ratecard, api.get_tour_company, api.archive_tour_company):
            with self.subTest(endpoint=call.__name__):
                with self.assertRaises(frappe.DoesNotExistError):
                    call(name="_LFT Blue Wave")

    def test_saving_the_name_again_brings_it_back(self):
        self.save(discount=10)
        api.archive_tour_company(name="_LFT Blue Wave")
        self.save(discount=25)
        self.assertEqual(self.listed()["_LFT Blue Wave"]["discount"], 25)

    def test_an_unknown_company_is_an_error_not_a_crash(self):
        with self.assertRaises(frappe.DoesNotExistError):
            api.archive_tour_company(name="_LFT Nobody")


class TestPrintingASavedCompany(SavedCompanyTestCase):
    def page(self, **kw):
        api.print_ratecard(name="_LFT Blue Wave", **kw)
        return frappe.local.response["filecontent"]

    def test_the_card_is_the_companys_with_its_discount(self):
        self.save(discount=10, items=("K1", "B1"))
        html = self.page()
        self.assertIn("<h1>Special Rates for _LFT Blue Wave</h1>", html)
        self.assertIn(">900<", html)          # 1000 less 10%
        self.assertIn(">410<", html)          # 455 less 10%, rounded up
        self.assertNotIn("Cheese Kottu", html)
        self.assertIn("+ 10% Service Charge will be added", html)
        self.assertEqual(frappe.local.response["filename"], "rate-card-lft-blue-wave.html")

    def test_the_pdf_link_names_the_company_and_needs_no_post(self):
        self.save()
        html = self.page()
        self.assertIn("print_ratecard?format=pdf&amp;name=_LFT%20Blue%20Wave", html)
        self.assertNotIn('name="csrf_token"', html)

    def test_the_pdf_is_the_printed_page(self):
        self.save()
        with mock.patch("frappe.utils.pdf.get_pdf", return_value=b"%PDF-x") as pdf:
            self.page(format="pdf")
        self.assertIn("Special Rates for _LFT Blue Wave", pdf.call_args[0][0])
        self.assertNotIn("Download PDF", pdf.call_args[0][0])
        self.assertTrue(frappe.local.response["filename"].endswith(".pdf"))

    def test_a_menu_price_change_reaches_the_saved_card(self):
        self.save(items=("K1",))
        self.assertIn(">900<", self.page())
        repriced = [dict(e, rate=2000) if e["code"] == "K1" else dict(e) for e in MENU]
        with mock.patch("luuvcrm.api.ratecard.menu.menu_items", return_value=repriced):
            self.assertIn(">1,800<", self.page())

    def test_an_item_added_to_the_menu_later_is_not_on_the_card_until_ticked(self):
        self.save(items=("K1",))
        grown = [dict(e) for e in MENU] + [{"code": "N1", "name": "Brand New Dish", "rate": 500, "group": "Kottu"}]
        with mock.patch("luuvcrm.api.ratecard.menu.menu_items", return_value=grown):
            self.assertNotIn("Brand New Dish", self.page())

    def test_a_company_whose_items_have_all_left_the_menu_says_so(self):
        self.save(items=("K1",))
        with mock.patch("luuvcrm.api.ratecard.menu.menu_items", return_value=[dict(MENU[2])]):
            with self.assertRaises(frappe.ValidationError) as ctx:
                self.page()
            self.assertIn("none of", str(ctx.exception).lower())
            self.assertEqual(self.listed()["_LFT Blue Wave"]["on_menu"], 0)


class TestTheManagerPage(FrappeTestCase):
    """The tab is strings in a page nobody re-reads: a renamed endpoint fails as
    a dead button on the manager's phone, not as an error."""

    def setUp(self):
        super().setUp()
        self.html = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "manager.html"))

    def test_the_tab_the_menu_row_and_the_switch_are_all_there(self):
        self.assertIn('id="tabRatecard"', self.html)
        self.assertIn("moreGo('ratecard')", self.html)
        self.assertIn("t==='ratecard'?'block':'none'", self.html)
        self.assertIn("t==='ratecard') loadRatecard()", self.html)

    def test_the_more_menu_still_lights_up_for_it(self):
        """The bottom bar has no button of its own for the tab; without this it
        would leave nothing highlighted."""
        self.assertIn("t==='ratecard')?'more':t", self.html)

    def test_it_talks_to_endpoints_that_exist(self):
        for name in ("get_ratecard_menu", "print_ratecard"):
            self.assertIn(f"luuvcrm.api.{name}", self.html)
            frappe.is_whitelisted(frappe.get_attr(f"luuvcrm.api.{name}"))

    def test_the_result_opens_in_a_new_tab_through_a_post_with_the_token(self):
        self.assertIn("f.method='POST'; f.target='_blank'", self.html)
        self.assertIn("['csrf_token',CSRF]", self.html)

    def test_the_form_asks_for_exactly_what_the_endpoint_takes(self):
        import inspect
        # `format` and `name` are the saved-card options; the preview form sends the rest.
        for field in inspect.signature(ratecard.print_ratecard).parameters:
            if field in ("format", "name"):
                continue
            self.assertIn(f"['{field}'," if field != "company" else "['company'", self.html)
