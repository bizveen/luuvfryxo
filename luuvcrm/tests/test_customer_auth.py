"""How a customer gets into /cus — registering, signing in, and the WhatsApp code.

This is the front door. It is `allow_guest` end to end, it is the only thing
standing between a phone number and somebody's order history and loyalty
balance, and the credential behind it is four digits — a 10,000-key space. So
what these tests pin down is less "does it work" than "what does it refuse":

* the PIN is stored salted and hashed, never as typed;
* a wrong PIN and an unknown number give the same answer, so the endpoint
  cannot be used to enumerate which numbers have an account;
* five wrong tries lock the number out for fifteen minutes;
* the token, not the mobile in the request, is what identifies the customer,
  and it stops working after seven days.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api
from luuvcrm.tests import fixtures as fx


class CustomerAuthTestCase(FrappeTestCase):
    """Calls the endpoints the way a request does — through form_dict.

    Every one of these reads `frappe.local.form_dict` rather than taking
    arguments, so calling them directly with keywords would test a signature
    nothing uses.
    """

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint()
        finally:
            frappe.local.form_dict = previous

    def stored_pin(self, mobile):
        name = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")
        return frappe.db.get_value("Customer Account", name, "pin")


# ─── Registering ────────────────────────────────────────────────────────────


class TestAskingWhetherAnAccountExists(CustomerAuthTestCase):
    """`customer_create_or_login` is the first screen: type a number, and the
    app decides whether to ask for a PIN or to set one."""

    MOBILE = "0770000201"

    def test_an_unknown_number_is_offered_registration(self):
        fx.drop_customer(self, self.MOBILE)
        result = self.call(api.customer_create_or_login, mobile=self.MOBILE)
        self.assertTrue(result["success"])
        self.assertFalse(result["exists"])

    def test_a_known_number_is_asked_for_its_pin(self):
        fx.customer_account(self, self.MOBILE, name="Nimal")
        result = self.call(api.customer_create_or_login, mobile=self.MOBILE)
        self.assertTrue(result["exists"])
        self.assertEqual(result["name"], "Nimal")

    def test_it_answers_with_a_name_and_nothing_else(self):
        """It says an account exists, which it has to — but it must not hand
        back the PIN, the hash, or a token on the strength of a number alone."""
        fx.customer_account(self, self.MOBILE, pin="4321", name="Nimal")
        result = self.call(api.customer_create_or_login, mobile=self.MOBILE)
        self.assertEqual(set(result), {"success", "exists", "name"})

    def test_a_short_number_is_refused(self):
        result = self.call(api.customer_create_or_login, mobile="0771")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Valid phone number required")


class TestSettingAPin(CustomerAuthTestCase):
    MOBILE = "0770000202"

    def setUp(self):
        super().setUp()
        fx.drop_customer(self, self.MOBILE)
        fx.forget_customer_cache(self.MOBILE)

    def test_it_creates_the_account_and_signs_the_customer_straight_in(self):
        result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="Kamala")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "Kamala")
        self.assertEqual(api._mobile_from_token(result["token"]), self.MOBILE,
                         "the token it returns must already be usable")

    def test_the_pin_is_never_stored_as_typed(self):
        """S6. A 4-digit PIN in a leaked table is no protection at all; the
        salted hash is what makes the table worth less than the account."""
        self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="Kamala")
        stored = self.stored_pin(self.MOBILE)
        self.assertNotEqual(stored, "1234")
        self.assertIn("$", stored, "expected the salt$hash form")
        salt, digest = stored.split("$", 1)
        self.assertTrue(salt and digest)
        self.assertEqual(api._hash_pin("1234", salt), stored)

    def test_two_accounts_with_the_same_pin_do_not_share_a_hash(self):
        """The salt is per-account, so the table cannot be scanned for everyone
        who picked 1234."""
        other = "0770000203"
        fx.drop_customer(self, other)
        self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="A")
        self.call(api.customer_set_pin, mobile=other, pin="1234", name="B")
        self.assertNotEqual(self.stored_pin(self.MOBILE), self.stored_pin(other))

    def test_a_pin_that_is_not_four_digits_is_refused(self):
        for bad in ("123", "12345", "abcd", "12a4", ""):
            with self.subTest(pin=bad):
                result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin=bad, name="X")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "PIN must be 4 digits")
        self.assertFalse(frappe.db.exists("Customer Account", {"mobile": self.MOBILE}))

    def test_a_short_number_is_refused(self):
        result = self.call(api.customer_set_pin, mobile="0771", pin="1234", name="X")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Valid phone required")

    def test_it_will_not_overwrite_an_existing_account(self):
        """Otherwise setting a PIN would be a password reset that anyone who
        knows the number can perform."""
        fx.customer_account(self, self.MOBILE, pin="1111", name="Kamala")
        before = self.stored_pin(self.MOBILE)
        result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin="9999", name="Attacker")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Account already exists")
        self.assertEqual(self.stored_pin(self.MOBILE), before)


class TestTheWhatsAppGateOnRegistration(CustomerAuthTestCase):
    """`customer_set_pin` will not finish while a code is outstanding.

    It is a soft gate on purpose: no code sent means WhatsApp is down or absent,
    and a restaurant losing its WhatsApp account must not lose registration
    with it. What it does stop is a client that asks for a code and then skips
    the screen.
    """

    MOBILE = "0770000204"

    def setUp(self):
        super().setUp()
        fx.drop_customer(self, self.MOBILE)
        fx.forget_customer_cache(self.MOBILE)

    def test_registration_goes_through_when_no_code_was_ever_sent(self):
        result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="Sunil")
        self.assertTrue(result["success"])

    def test_an_unconfirmed_code_blocks_registration(self):
        with patch.object(api, "_send_wa_reply", return_value=True):
            self.call(api.wa_send_otp, mobile=self.MOBILE)
        result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="Sunil")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Confirm the WhatsApp code first")
        self.assertFalse(frappe.db.exists("Customer Account", {"mobile": self.MOBILE}))

    def test_confirming_the_code_lets_registration_through(self):
        sent = {}
        with patch.object(api, "_send_wa_reply", return_value=True) as send:
            self.call(api.wa_send_otp, mobile=self.MOBILE)
            sent["body"] = send.call_args[0][1]
        code = _code_from(sent["body"])
        self.assertTrue(self.call(api.wa_verify_otp, mobile=self.MOBILE, code=code)["ok"])
        result = self.call(api.customer_set_pin, mobile=self.MOBILE, pin="1234", name="Sunil")
        self.assertTrue(result["success"])


# ─── Signing in ─────────────────────────────────────────────────────────────


class TestSigningIn(CustomerAuthTestCase):
    MOBILE = "0770000205"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE, pin="1234", name="Nimal")
        fx.forget_customer_cache(self.MOBILE)

    def test_the_right_pin_returns_a_working_token(self):
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "Nimal")
        self.assertEqual(api._mobile_from_token(result["token"]), self.MOBILE)

    def test_signing_in_twice_reuses_the_same_token(self):
        """/cus keeps the token in local storage; a fresh one per sign-in would
        sign the customer out on their other device."""
        first = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")["token"]
        second = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")["token"]
        self.assertEqual(first, second)

    def test_it_records_the_sign_in(self):
        frappe.db.set_value("Customer Account",
                            frappe.db.get_value("Customer Account", {"mobile": self.MOBILE}, "name"),
                            "last_login", None)
        self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        self.assertTrue(frappe.db.get_value("Customer Account",
                                            {"mobile": self.MOBILE}, "last_login"))

    def test_a_wrong_pin_is_refused(self):
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="9999")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Wrong PIN")
        self.assertNotIn("token", result)

    def test_an_unknown_number_answers_exactly_like_a_wrong_pin(self):
        """Otherwise the endpoint is a directory: try numbers until the error
        text changes, and you know which ones are customers."""
        unknown = self.call(api.customer_verify_pin, mobile="0770000299", pin="1234")
        wrong = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="9999")
        self.addCleanup(fx.forget_customer_cache, "0770000299")
        self.assertEqual(unknown, wrong)

    def test_both_a_number_and_a_pin_are_required(self):
        for kwargs in ({"mobile": self.MOBILE, "pin": ""}, {"mobile": "", "pin": "1234"}):
            with self.subTest(**kwargs):
                result = self.call(api.customer_verify_pin, **kwargs)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "Phone and PIN required")


class TestUpgradingOldPins(CustomerAuthTestCase):
    """Accounts made before S6 hold the PIN in plaintext. They must keep
    working, and drain out of the table as people sign in."""

    MOBILE = "0770000206"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE, pin="1234", name="Old", hashed=False)
        fx.forget_customer_cache(self.MOBILE)

    def test_a_plaintext_pin_still_signs_in(self):
        self.assertEqual(self.stored_pin(self.MOBILE), "1234")
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        self.assertTrue(result["success"])

    def test_and_is_hashed_on_the_way_through(self):
        self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        stored = self.stored_pin(self.MOBILE)
        self.assertNotEqual(stored, "1234")
        salt = stored.split("$", 1)[0]
        self.assertEqual(api._hash_pin("1234", salt), stored)

    def test_the_upgraded_pin_still_works_next_time(self):
        self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        fx.forget_customer_cache(self.MOBILE)
        self.assertTrue(self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")["success"])

    def test_a_wrong_pin_against_a_plaintext_account_is_still_wrong(self):
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="4321")
        self.assertFalse(result["success"])
        self.assertEqual(self.stored_pin(self.MOBILE), "1234",
                         "a failed attempt must not rewrite the stored PIN")


class TestTheLockout(CustomerAuthTestCase):
    """Four digits is 10,000 guesses. The lockout is what makes that a wall."""

    MOBILE = "0770000207"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE, pin="1234", name="Nimal")
        fx.forget_customer_cache(self.MOBILE)
        self.addCleanup(fx.forget_customer_cache, self.MOBILE)

    def wrong(self, times):
        for _ in range(times):
            self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="9999")

    def test_the_counter_actually_counts(self):
        """It has not always. get_value() without expires=True remembers the
        miss in frappe.local.cache while set_value(expires_in_sec=...)
        deliberately does not update it, so the count sat at 1 forever."""
        self.wrong(3)
        self.assertEqual(api._pin_fails(self.MOBILE), 3)

    def test_five_wrong_pins_lock_the_number(self):
        self.wrong(api.PIN_MAX_ATTEMPTS)
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="9999")
        self.assertEqual(result["error"], "Too many wrong PINs — try again in 15 minutes")

    def test_the_lockout_holds_even_against_the_right_pin(self):
        """The point of a lockout is that it does not open for whoever guesses
        correctly on the 5,001st try."""
        self.wrong(api.PIN_MAX_ATTEMPTS)
        result = self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        self.assertFalse(result["success"])
        self.assertNotIn("token", result)

    def test_four_wrong_tries_still_leave_the_door_open(self):
        """Someone mistyping their own PIN must not be locked out early."""
        self.wrong(api.PIN_MAX_ATTEMPTS - 1)
        self.assertTrue(self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")["success"])

    def test_signing_in_clears_the_count(self):
        self.wrong(3)
        self.call(api.customer_verify_pin, mobile=self.MOBILE, pin="1234")
        self.assertEqual(api._pin_fails(self.MOBILE), 0)

    def test_guessing_at_an_unknown_number_is_counted_too(self):
        """A number with no account gets the same treatment, so the lockout
        cannot be sidestepped by scanning numbers rather than PINs."""
        unknown = "0770000298"
        self.addCleanup(fx.forget_customer_cache, unknown)
        fx.forget_customer_cache(unknown)
        for _ in range(api.PIN_MAX_ATTEMPTS):
            self.call(api.customer_verify_pin, mobile=unknown, pin="1234")
        result = self.call(api.customer_verify_pin, mobile=unknown, pin="1234")
        self.assertEqual(result["error"], "Too many wrong PINs — try again in 15 minutes")

    def test_one_number_being_locked_does_not_lock_another(self):
        other = "0770000208"
        fx.customer_account(self, other, pin="1234", name="Other")
        self.addCleanup(fx.forget_customer_cache, other)
        fx.forget_customer_cache(other)
        self.wrong(api.PIN_MAX_ATTEMPTS)
        self.assertTrue(self.call(api.customer_verify_pin, mobile=other, pin="1234")["success"])


# ─── The token, and what it opens ───────────────────────────────────────────


class TestTheToken(CustomerAuthTestCase):
    MOBILE = "0770000209"

    def setUp(self):
        super().setUp()
        fx.customer_account(self, self.MOBILE, pin="1234", name="Nimal")

    def test_a_token_names_its_customer(self):
        token = fx.customer_token(self, self.MOBILE)
        self.assertEqual(api._mobile_from_token(token), self.MOBILE)

    def test_a_made_up_token_names_nobody(self):
        self.assertIsNone(api._mobile_from_token("deadbeef" * 4))

    def test_an_empty_token_names_nobody(self):
        for empty in ("", None, "   "):
            with self.subTest(token=empty):
                self.assertFalse(api._mobile_from_token(empty))

    def test_a_token_older_than_a_week_stops_working(self):
        """S8. The lazy DELETE in _get_or_create_customer_token only runs when
        somebody else signs in, so on a quiet week an abandoned token used to
        stay valid indefinitely. The expiry is enforced on read instead."""
        stale = fx.customer_token(self, self.MOBILE, age_days=api.CUSTOMER_TOKEN_DAYS + 1)
        self.assertIsNone(api._mobile_from_token(stale))

    def test_a_token_just_inside_the_week_still_works(self):
        fresh = fx.customer_token(self, self.MOBILE, age_days=api.CUSTOMER_TOKEN_DAYS - 1)
        self.assertEqual(api._mobile_from_token(fresh), self.MOBILE)


class TestTheProfile(CustomerAuthTestCase):
    """`customer_get_profile` / `customer_update_profile` drive the
    complete-your-profile prompt. Both are allow_guest; the token is the auth."""

    MOBILE = "0770000210"

    def setUp(self):
        super().setUp()
        fx.customer_profile_fields()
        fx.customer_account(self, self.MOBILE, pin="1234", name="Nimal")
        self.token = fx.customer_token(self, self.MOBILE)

    def test_reading_a_profile_needs_a_token(self):
        result = self.call(api.customer_get_profile)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Login required")

    def test_writing_a_profile_needs_a_token(self):
        result = self.call(api.customer_update_profile, name="Attacker", address="anywhere")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Login required")

    def test_a_mobile_in_the_request_is_not_a_credential(self):
        """The same hole S3 closed on customer_get_orders and the loyalty pass
        closed on the balance: knowing a number must open nothing."""
        result = self.call(api.customer_get_profile, mobile=self.MOBILE)
        self.assertFalse(result["success"])

    def test_an_expired_token_is_refused(self):
        stale = fx.customer_token(self, self.MOBILE, age_days=api.CUSTOMER_TOKEN_DAYS + 1)
        self.assertFalse(self.call(api.customer_get_profile, token=stale)["success"])
        self.assertFalse(self.call(api.customer_update_profile, token=stale, name="X")["success"])

    def test_a_new_profile_is_reported_incomplete(self):
        result = self.call(api.customer_get_profile, token=self.token)
        self.assertTrue(result["success"])
        self.assertEqual(result["mobile"], self.MOBILE)
        self.assertFalse(result["profile_complete"])

    def test_filling_it_in_completes_it(self):
        self.assertTrue(self.call(api.customer_update_profile, token=self.token,
                                  name="Nimal Perera", address="12 Galle Rd",
                                  area="Wellawatte")["success"])
        result = self.call(api.customer_get_profile, token=self.token)
        self.assertEqual(result["name"], "Nimal Perera")
        self.assertEqual(result["address"], "12 Galle Rd")
        self.assertEqual(result["area"], "Wellawatte")
        self.assertTrue(result["profile_complete"])

    def test_a_partly_filled_profile_is_still_incomplete(self):
        self.call(api.customer_update_profile, token=self.token,
                  name="Nimal Perera", address="12 Galle Rd")
        self.assertFalse(self.call(api.customer_get_profile, token=self.token)["profile_complete"])

    def test_blank_fields_leave_what_is_there_alone(self):
        """The app posts the whole form; an untouched field arrives empty and
        must not wipe the address the customer typed last week."""
        self.call(api.customer_update_profile, token=self.token, name="Nimal Perera",
                  address="12 Galle Rd", area="Wellawatte")
        self.call(api.customer_update_profile, token=self.token, name="", address="", area="")
        result = self.call(api.customer_get_profile, token=self.token)
        self.assertEqual(result["address"], "12 Galle Rd")
        self.assertEqual(result["area"], "Wellawatte")

    def test_a_token_whose_account_is_gone_is_not_a_way_in(self):
        orphan = "0770000211"
        token = fx.customer_token(self, orphan)
        self.assertEqual(self.call(api.customer_get_profile, token=token)["error"],
                         "Account not found")
        self.assertEqual(self.call(api.customer_update_profile, token=token, name="X")["error"],
                         "Account not found")


# ─── The WhatsApp code ──────────────────────────────────────────────────────


def _code_from(message):
    """Pull the 4-digit code out of the WhatsApp body the endpoint composed."""
    import re
    match = re.search(r"\*(\d{4})\*", message or "")
    assert match, f"no code in the message: {message!r}"
    return match.group(1)


class TestSendingTheCode(CustomerAuthTestCase):
    MOBILE = "0770000212"

    def setUp(self):
        super().setUp()
        fx.forget_customer_cache(self.MOBILE)
        self.addCleanup(fx.forget_customer_cache, self.MOBILE)

    def test_it_sends_a_four_digit_code_to_the_international_number(self):
        with patch.object(api, "_send_wa_reply", return_value=True) as send:
            result = self.call(api.wa_send_otp, mobile=self.MOBILE)
        self.assertTrue(result["sent"])
        to, body = send.call_args[0]
        self.assertEqual(to, "94" + self.MOBILE[1:],
                         "the Cloud API wants 947XXXXXXXX, not the local form")
        self.assertEqual(_code_from(body), frappe.cache().get_value(f"wa_otp:{self.MOBILE}"))

    def test_a_short_number_is_refused_before_anything_is_sent(self):
        with patch.object(api, "_send_wa_reply", return_value=True) as send:
            result = self.call(api.wa_send_otp, mobile="0771")
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "Valid mobile required")
        send.assert_not_called()

    def test_nothing_is_cached_when_whatsapp_will_not_take_it(self):
        """A site with no WhatsApp Account gets False from _send_wa_reply. If a
        code were cached anyway, the soft gate on registration would then block
        a customer who can never receive one."""
        with patch.object(api, "_send_wa_reply", return_value=False):
            result = self.call(api.wa_send_otp, mobile=self.MOBILE)
        self.assertFalse(result["sent"])
        self.assertIsNone(frappe.cache().get_value(f"wa_otp:{self.MOBILE}", expires=True))

    def test_a_second_request_inside_the_cooldown_reuses_the_first_code(self):
        """Tapping Resend twice must not invalidate the code already on the
        customer's phone — and must not send two messages."""
        with patch.object(api, "_send_wa_reply", return_value=True) as send:
            self.call(api.wa_send_otp, mobile=self.MOBILE)
            first = _code_from(send.call_args[0][1])
            result = self.call(api.wa_send_otp, mobile=self.MOBILE)
            self.assertEqual(send.call_count, 1)
        self.assertTrue(result["sent"])
        self.assertTrue(result["cooldown"])
        self.assertTrue(self.call(api.wa_verify_otp, mobile=self.MOBILE, code=first)["ok"])


class TestConfirmingTheCode(CustomerAuthTestCase):
    MOBILE = "0770000213"

    def setUp(self):
        super().setUp()
        fx.forget_customer_cache(self.MOBILE)
        self.addCleanup(fx.forget_customer_cache, self.MOBILE)
        with patch.object(api, "_send_wa_reply", return_value=True) as send:
            self.call(api.wa_send_otp, mobile=self.MOBILE)
            self.code = _code_from(send.call_args[0][1])

    def test_the_right_code_marks_the_number_confirmed(self):
        self.assertTrue(self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)["ok"])
        self.assertTrue(frappe.cache().get_value(f"wa_verified:{self.MOBILE}", expires=True))

    def test_the_code_is_spent_once(self):
        """Replaying it must not confirm the number a second time."""
        self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)
        again = self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"], "Code expired — tap Resend")

    def test_a_wrong_code_is_refused(self):
        wrong = "0000" if self.code != "0000" else "1111"
        result = self.call(api.wa_verify_otp, mobile=self.MOBILE, code=wrong)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Wrong code — check WhatsApp")
        self.assertFalse(frappe.cache().get_value(f"wa_verified:{self.MOBILE}", expires=True))

    def test_a_wrong_code_does_not_burn_the_right_one(self):
        wrong = "0000" if self.code != "0000" else "1111"
        self.call(api.wa_verify_otp, mobile=self.MOBILE, code=wrong)
        self.assertTrue(self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)["ok"])

    def test_five_wrong_codes_stop_the_guessing(self):
        """Four digits again: without this, the code is guessable inside its
        five-minute life."""
        wrong = "0000" if self.code != "0000" else "1111"
        for _ in range(5):
            self.call(api.wa_verify_otp, mobile=self.MOBILE, code=wrong)
        result = self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)
        self.assertFalse(result["ok"], "the right code must not open a burnt-out attempt count")
        self.assertEqual(result["error"], "Too many attempts — request a new code")

    def test_confirming_clears_the_attempt_count(self):
        wrong = "0000" if self.code != "0000" else "1111"
        self.call(api.wa_verify_otp, mobile=self.MOBILE, code=wrong)
        self.call(api.wa_verify_otp, mobile=self.MOBILE, code=self.code)
        self.assertIsNone(frappe.cache().get_value(f"wa_otp_tries:{self.MOBILE}", expires=True))

    def test_a_number_with_no_code_outstanding(self):
        other = "0770000214"
        self.addCleanup(fx.forget_customer_cache, other)
        fx.forget_customer_cache(other)
        result = self.call(api.wa_verify_otp, mobile=other, code="1234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Code expired — tap Resend")

    def test_both_a_number_and_a_code_are_required(self):
        for kwargs in ({"mobile": self.MOBILE, "code": ""}, {"mobile": "", "code": "1234"}):
            with self.subTest(**kwargs):
                result = self.call(api.wa_verify_otp, **kwargs)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "Code required")
