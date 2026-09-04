"""Customer loyalty: points, referrals, and spending the balance.

The balance is money the restaurant owes a customer, and until this pass every
one of these endpoints was `allow_guest` and took the customer's phone number
straight from the request. Knowing a number was enough to read somebody's
balance and referral network — and, through `loyalty_redeem`, to spend it.

So the first thing these tests pin is that the token is the only way in.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


def _customer(case, mobile, balance=0.0, name=None):
    """A Loyalty Customer with a balance, and a token to reach it with."""
    api._install_loyalty() if hasattr(api, "_install_loyalty") else api.setup_loyalty()
    lc_name = api._get_loyalty_customer(mobile)
    doc = frappe.get_doc("Loyalty Customer", lc_name)
    doc.cashback_balance = balance
    if name:
        doc.customer_name = name
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)

    def drop():
        for other in frappe.get_all("Loyalty Customer",
                                    filters={"referred_by": lc_name}, pluck="name"):
            frappe.db.set_value("Loyalty Customer", other, "referred_by", None)
        if frappe.db.exists("Loyalty Customer", lc_name):
            frappe.delete_doc("Loyalty Customer", lc_name, force=True, ignore_permissions=True)
        for tok in frappe.get_all("Customer Token", filters={"mobile": mobile}, pluck="name"):
            frappe.delete_doc("Customer Token", tok, force=True, ignore_permissions=True)

    case.addCleanup(drop)
    return lc_name


def _token(case, mobile):
    api.setup_customer_accounts()
    token = api._get_or_create_customer_token(mobile)
    case.addCleanup(lambda: [
        frappe.delete_doc("Customer Token", t, force=True, ignore_permissions=True)
        for t in frappe.get_all("Customer Token", filters={"mobile": mobile}, pluck="name")
        if frappe.db.exists("Customer Token", t)])
    return token


class LoyaltyTestCase(FrappeTestCase):
    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous


class TestOnlyTheTokenGetsIn(LoyaltyTestCase):
    """The hole this pass closed: a phone number was the whole credential."""

    MOBILE = "0770000101"

    def setUp(self):
        super().setUp()
        _customer(self, self.MOBILE, balance=1000)

    def test_a_bare_phone_number_no_longer_reads_a_balance(self):
        result = self.call(api.loyalty_get_info, mobile=self.MOBILE)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Login required")

    def test_a_bare_phone_number_no_longer_spends_a_balance(self):
        """The worst of them: this moved money."""
        result = self.call(api.loyalty_redeem, mobile=self.MOBILE, amount=100)
        self.assertFalse(result["success"])
        self.assertEqual(
            float(frappe.db.get_value("Loyalty Customer", {"mobile": self.MOBILE},
                                      "cashback_balance")), 1000,
            "the balance must be untouched")

    def test_a_bare_phone_number_no_longer_mints_a_referral_code(self):
        self.assertFalse(self.call(api.loyalty_generate_code, mobile=self.MOBILE)["success"])

    def test_a_made_up_token_gets_nothing(self):
        self.assertFalse(self.call(api.loyalty_get_info, token="not-a-token")["success"])

    def test_the_right_token_gets_in(self):
        token = _token(self, self.MOBILE)
        result = self.call(api.loyalty_get_info, token=token)
        self.assertTrue(result["success"])
        self.assertEqual(result["mobile"], self.MOBILE)


class TestReadingTheAccount(LoyaltyTestCase):
    MOBILE = "0770000102"

    def setUp(self):
        super().setUp()
        _customer(self, self.MOBILE, balance=750, name="Nimal")
        self.token = _token(self, self.MOBILE)

    def test_it_reports_the_balance_and_the_name(self):
        result = self.call(api.loyalty_get_info, token=self.token)
        self.assertEqual(result["customer_name"], "Nimal")
        self.assertEqual(result["cashback_balance"], 750)

    def test_an_account_that_does_not_exist_says_so(self):
        other = "0770000199"
        token = _token(self, other)
        result = self.call(api.loyalty_get_info, token=token)
        self.assertFalse(result["success"])
        self.assertFalse(result["has_account"])

    def test_a_referrer_sees_who_they_brought_in(self):
        referrer = _customer(self, self.MOBILE)
        invitee = _customer(self, "0770000103")
        frappe.db.set_value("Loyalty Customer", invitee, "referred_by", referrer)
        result = self.call(api.loyalty_get_info, token=self.token)
        self.assertIn("0770000103", [d["mobile"] for d in result["downline"]])


class TestReferralCodes(LoyaltyTestCase):
    MOBILE = "0770000104"

    def setUp(self):
        super().setUp()
        _customer(self, self.MOBILE)
        self.token = _token(self, self.MOBILE)

    def test_a_code_is_minted_on_request(self):
        code = self.call(api.loyalty_generate_code, token=self.token)["referral_code"]
        self.assertTrue(code)

    def test_asking_twice_returns_the_same_code(self):
        """A code that changed would break every link already shared."""
        first = self.call(api.loyalty_generate_code, token=self.token)["referral_code"]
        second = self.call(api.loyalty_generate_code, token=self.token)["referral_code"]
        self.assertEqual(first, second)

    def test_a_code_leads_back_to_its_owner(self):
        code = self.call(api.loyalty_generate_code, token=self.token)["referral_code"]
        found = self.call(api.loyalty_find_referrer, ref_code=code)
        self.assertTrue(found["found"])

    def test_lookup_is_case_insensitive(self):
        code = self.call(api.loyalty_generate_code, token=self.token)["referral_code"]
        self.assertTrue(self.call(api.loyalty_find_referrer, ref_code=code.lower())["found"])

    def test_an_unknown_code_finds_nobody(self):
        self.assertFalse(self.call(api.loyalty_find_referrer, ref_code="_LFTZZZ")["found"])

    def test_an_empty_code_finds_nobody(self):
        self.assertFalse(self.call(api.loyalty_find_referrer, ref_code="")["found"])


class TestApplyingAReferral(LoyaltyTestCase):
    REFERRER = "0770000105"
    INVITEE = "0770000106"

    def setUp(self):
        super().setUp()
        self.referrer = _customer(self, self.REFERRER)
        _customer(self, self.INVITEE)
        self.referrer_token = _token(self, self.REFERRER)
        self.invitee_token = _token(self, self.INVITEE)
        self.code = self.call(api.loyalty_generate_code,
                              token=self.referrer_token)["referral_code"]

    def test_a_referral_links_the_two_accounts(self):
        result = self.call(api.loyalty_apply_referral,
                           token=self.invitee_token, ref_code=self.code)
        self.assertTrue(result["success"])
        self.assertEqual(frappe.db.get_value("Loyalty Customer",
                                             {"mobile": self.INVITEE}, "referred_by"),
                         self.referrer)

    def test_nobody_refers_themselves(self):
        result = self.call(api.loyalty_apply_referral,
                           token=self.referrer_token, ref_code=self.code)
        self.assertFalse(result["success"])
        self.assertIn("refer yourself", result["error"])

    def test_a_customer_is_referred_once(self):
        """The one-shot guard is what keeps the fallback below from being a way
        to reassign somebody else's referrer."""
        self.call(api.loyalty_apply_referral, token=self.invitee_token, ref_code=self.code)
        again = self.call(api.loyalty_apply_referral, token=self.invitee_token,
                          ref_code=self.code)
        self.assertFalse(again["success"])
        self.assertIn("Already linked", again["error"])

    def test_an_unknown_code_is_refused(self):
        result = self.call(api.loyalty_apply_referral,
                           token=self.invitee_token, ref_code="_LFTZZZ")
        self.assertFalse(result["success"])
        self.assertIn("Invalid referral code", result["error"])

    def test_a_guest_checkout_can_still_attach_a_referral(self):
        """Deliberate: /cus lets a customer order without an account, and the
        referral has to attach before the order or the referrer earns nothing.
        This endpoint only writes a link — it reads no balance and spends
        nothing — and the one-shot guard above limits the damage."""
        result = self.call(api.loyalty_apply_referral,
                           mobile=self.INVITEE, ref_code=self.code)
        self.assertTrue(result["success"])


class TestSpendingTheBalance(LoyaltyTestCase):
    MOBILE = "0770000107"

    def setUp(self):
        super().setUp()
        _customer(self, self.MOBILE, balance=1000)
        self.token = _token(self, self.MOBILE)
        self.settings = api._get_loyalty_settings()

    def balance(self):
        return float(frappe.db.get_value("Loyalty Customer",
                                         {"mobile": self.MOBILE}, "cashback_balance"))

    def test_redeeming_takes_it_off_the_balance(self):
        amount = max(self.settings["min_redeem"], 100)
        result = self.call(api.loyalty_redeem, token=self.token, amount=amount)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(self.balance(), 1000 - amount)

    def test_spending_more_than_the_balance_is_refused(self):
        result = self.call(api.loyalty_redeem, token=self.token, amount=5000)
        self.assertFalse(result["success"])
        self.assertIn("Insufficient balance", result["error"])
        self.assertEqual(self.balance(), 1000)

    def test_below_the_minimum_is_refused(self):
        minimum = self.settings["min_redeem"]
        if minimum <= 0:
            self.skipTest("no minimum configured on this site")
        result = self.call(api.loyalty_redeem, token=self.token, amount=minimum - 1)
        self.assertFalse(result["success"])
        self.assertIn("Minimum redeem", result["error"])
        self.assertEqual(self.balance(), 1000)

    def test_redeeming_nothing_is_refused(self):
        for amount in (0, -50):
            with self.subTest(amount=amount):
                self.assertFalse(self.call(api.loyalty_redeem,
                                           token=self.token, amount=amount)["success"])
        self.assertEqual(self.balance(), 1000)

    def test_what_was_spent_is_totalled(self):
        amount = max(self.settings["min_redeem"], 100)
        self.call(api.loyalty_redeem, token=self.token, amount=amount)
        self.assertEqual(
            float(frappe.db.get_value("Loyalty Customer", {"mobile": self.MOBILE},
                                      "total_redeemed")), amount)


class TestSettings(LoyaltyTestCase):
    """The scheme pays three levels deep: whoever referred you, whoever referred
    them, and one more. The percentage falls away with distance."""

    def test_the_scheme_reports_its_own_rules(self):
        result = self.call(api.loyalty_get_settings_api)
        for key in ("level_1_percent", "level_2_percent", "level_3_percent",
                    "min_redeem", "referral_discount_percent"):
            self.assertIn(key, result)

    def test_each_level_pays_less_than_the_one_above(self):
        s = api._get_loyalty_settings()
        self.assertGreaterEqual(s["level_1_percent"], s["level_2_percent"])
        self.assertGreaterEqual(s["level_2_percent"], s["level_3_percent"])

    def test_there_is_a_floor_under_a_redemption(self):
        self.assertGreater(api._get_loyalty_settings()["min_redeem"], 0)
