"""The staff activity trail, and what it must never write down.

Every mutating POST is logged with its arguments, which is what makes the trail
worth having — and exactly why it is a liability if it is careless. A PIN or an
OTP that reaches this table is a credential stored in plain text, readable by
anyone with the manager app, kept for ninety days.

`_sanitize` is the only thing standing between the request and the row.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import activity


class TestSanitize(FrappeTestCase):
    def test_it_keeps_the_ordinary_arguments(self):
        clean = activity._sanitize({"order_name": "POS-0001", "qty": 2})
        self.assertEqual(clean, {"order_name": "POS-0001", "qty": "2"})

    def test_every_declared_secret_is_dropped(self):
        payload = dict.fromkeys(activity.SECRET_KEYS, "s3cret")
        payload["order_name"] = "POS-0001"
        clean = activity._sanitize(payload)
        self.assertEqual(clean, {"order_name": "POS-0001"},
                         f"these reached the log: {sorted(set(clean) - {'order_name'})}")

    def test_the_obvious_credentials_are_covered(self):
        for key in ("pin", "new_pin", "password", "otp", "token", "csrf_token", "api_secret"):
            with self.subTest(key=key):
                self.assertNotIn(key, activity._sanitize({key: "1234"}))

    def test_anything_named_like_a_password_is_dropped(self):
        """The check is on the shape of the name, not only the exact list, so a
        field added later does not quietly start leaking."""
        for key in ("guide_password", "PASSWORD", "confirm_password", "old_password_hash"):
            with self.subTest(key=key):
                self.assertNotIn(key, activity._sanitize({key: "hunter2"}))

    def test_anything_ending_in_pin_is_dropped(self):
        for key in ("manager_pin", "customer_pin", "MANAGER_PIN"):
            with self.subTest(key=key):
                self.assertNotIn(key, activity._sanitize({key: "4321"}))

    def test_a_field_merely_containing_pin_is_kept(self):
        """`_pin` is matched as a suffix on purpose — dropping everything with
        those three letters in it would quietly lose real data."""
        self.assertIn("pinned_note", activity._sanitize({"pinned_note": "table 4"}))

    def test_nested_values_are_flattened_not_lost(self):
        clean = activity._sanitize({"items": [{"item": "BURGER", "qty": 2}]})
        self.assertIn("BURGER", clean["items"])

    def test_a_long_value_is_clipped(self):
        clean = activity._sanitize({"notes": "x" * 5000})
        self.assertLessEqual(len(clean["notes"]), 600)

    def test_a_long_key_is_clipped(self):
        clean = activity._sanitize({"k" * 200: "v"})
        self.assertLessEqual(max(len(k) for k in clean), 40)

    def test_something_that_is_not_a_payload_yields_nothing(self):
        for value in (None, "a string", 42, ["a", "list"]):
            with self.subTest(value=value):
                self.assertEqual(activity._sanitize(value), {})


class TestWhatIsLogged(FrappeTestCase):
    def test_the_loggers_never_log_themselves(self):
        """Otherwise reading the trail would write to the trail."""
        for method in ("log_ui_activity", "get_activity", "purge_activity_log"):
            self.assertIn(method, activity.NEVER_LOGGED)

    def test_endpoints_that_log_their_own_detail_are_not_logged_twice(self):
        """The money-moving endpoints write a richer row themselves, so the
        request hook must stay out of their way."""
        for method in ("process_payment", "pay_split", "pos_close_shift", "resolve_approval"):
            self.assertIn(method, activity.SELF_LOGGED)

    def test_the_two_lists_do_not_overlap(self):
        self.assertEqual(activity.SELF_LOGGED & activity.NEVER_LOGGED, set())


class TestWriting(FrappeTestCase):
    def setUp(self):
        super().setUp()
        activity.setup_activity_log()
        self._rows = []
        self.addCleanup(self._drop_rows)

    def _drop_rows(self):
        for name in self._rows:
            if frappe.db.exists(activity.DOCTYPE, name):
                frappe.delete_doc(activity.DOCTYPE, name, force=True, ignore_permissions=True)

    def log(self, **kwargs):
        name = activity.log_activity(**kwargs)
        if name:
            self._rows.append(name)
        return name

    def row(self, name):
        return frappe.get_doc(activity.DOCTYPE, name)

    def test_an_action_is_recorded_against_the_user_who_took_it(self):
        name = self.log(action="test_action", label="Test action")
        row = self.row(name)
        self.assertEqual(row.action, "test_action")
        self.assertEqual(row.user, frappe.session.user)

    def test_a_secret_in_the_detail_does_not_reach_the_row(self):
        name = self.log(action="test_secret",
                        detail={"pin": "4321", "order_name": "POS-0001"})
        row = self.row(name)
        self.assertNotIn("4321", row.detail or "")
        self.assertIn("POS-0001", row.detail or "")

    def test_a_flagged_action_is_marked_for_review(self):
        name = self.log(action="test_flag", flag_reason="big_discount",
                        flag_note="40% off")
        row = self.row(name)
        self.assertEqual(row.flag, 1)
        self.assertEqual(row.flag_reason, "big_discount")

    def test_an_ordinary_action_is_not_flagged(self):
        self.assertEqual(self.row(self.log(action="test_plain")).flag, 0)

    def test_an_unknown_app_is_bucketed_rather_than_stored_raw(self):
        row = self.row(self.log(action="test_app", app="not-an-app"))
        self.assertEqual(row.app, "other")

    def test_a_known_app_is_kept(self):
        row = self.row(self.log(action="test_app2", app="pos"))
        self.assertEqual(row.app, "pos")

    def test_logging_never_raises_whatever_it_is_given(self):
        """The caller is a live POS: a logger that throws breaks the app it
        watches."""
        self.log(action=None)
        self.log(action="test_odd", qty="not a number", amount=object())
        self.log(action="test_big", detail="x" * 100_000)
