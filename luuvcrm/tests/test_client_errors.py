"""The front-end error log — the thing built to say the app is broken.

`lferr.js` is the first script on every staff PWA and posts each front-end error
to `luuvcrm.api.log_client_error`. That endpoint did not exist. Because the
reporter is deliberately fire-and-forget inside a try/catch, every report 404'd
in silence, and `/errors` and the `/admin` badge answered nothing — so the alarm
had been off for its whole life and looked exactly like an app with no errors.

These tests exist because that failure mode is invisible by construction. They
pin the contract the pages already speak — the keys `/errors` renders, the
`open_count` `/admin` puts in a badge — and the two properties that decide
whether the log is worth reading at all: repeats collapse, and the writer never
throws at the page that called it.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.api import errors

DOCTYPE = errors.CLIENT_ERROR_DOCTYPE
MANAGER = "lf-test-errlog-manager@example.com"
CASHIER = "lf-test-errlog-cashier@example.com"


def _user(email, role):
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User", "email": email, "first_name": "Errlog",
            "send_welcome_email": 0, "roles": [{"role": role}],
        }).insert(ignore_permissions=True)
    return email


class ClientErrorTestCase(FrappeTestCase):
    APP = "_LFT-app"

    def setUp(self):
        super().setUp()
        errors._ensure_client_error_doctype()
        self._drop()
        self.addCleanup(self._drop)
        _user(MANAGER, roles.RESTAURANT_MANAGER)
        _user(CASHIER, roles.CASHIER)
        self.as_user(MANAGER)

    def _drop(self):
        for name in frappe.get_all(DOCTYPE, filters={"app": self.APP}, pluck="name"):
            frappe.delete_doc(DOCTYPE, name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def report(self, **payload):
        """Post one error the way lferr.js does — flat form fields."""
        payload.setdefault("app", self.APP)
        payload.setdefault("kind", "js")
        payload.setdefault("message", "Cannot read properties of undefined")
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(payload)
        try:
            return api.log_client_error()
        finally:
            frappe.local.form_dict = previous

    def rows(self, **kwargs):
        kwargs.setdefault("app", self.APP)
        return api.get_client_errors(**kwargs)


class TestRecordingAnError(ClientErrorTestCase):
    def test_an_error_is_stored_and_comes_back(self):
        self.assertTrue(self.report(message="boom", source="/pos", line=42)["ok"])
        rows = self.rows()["errors"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "boom")
        self.assertEqual(rows[0]["source"], "/pos")
        self.assertEqual(rows[0]["line"], 42)

    def test_it_carries_everything_the_page_shows(self):
        """/errors renders each of these; a missing key is a blank card."""
        self.report(message="boom", source="/pos.js", line=7, col=3, stack="at f()",
                    page="/pos?x=1", ua="Android 11 WebView", screen="1280x728")
        row = self.rows()["errors"][0]
        for key in ("name", "kind", "app", "message", "source", "line", "page", "user",
                    "stack", "ua", "screen", "hits", "status", "first_seen",
                    "last_seen", "when"):
            self.assertIn(key, row)
        self.assertEqual(row["stack"], "at f()")
        self.assertEqual(row["screen"], "1280x728")
        self.assertEqual(row["status"], "open")

    def test_it_records_who_hit_it(self):
        """A stack trace without the device or the person is a bug report you
        cannot follow up."""
        self.report(message="boom")
        self.assertEqual(self.rows()["errors"][0]["user"], MANAGER)

    def test_an_unknown_kind_is_bucketed_rather_than_stored_raw(self):
        self.report(message="boom", kind="<script>")
        self.assertEqual(self.rows()["errors"][0]["kind"], "js")

    def test_every_kind_lferr_sends_is_accepted(self):
        for i, kind in enumerate(("js", "promise", "resource", "console", "api", "manual")):
            with self.subTest(kind=kind):
                self.report(message=f"boom {i}", kind=kind)
        self.assertEqual({r["kind"] for r in self.rows()["errors"]},
                         set(errors.CLIENT_ERROR_KINDS))

    def test_a_report_with_no_message_is_not_stored(self):
        """There is nothing to show and nothing to group on."""
        self.assertFalse(self.report(message="   ")["ok"])
        self.assertEqual(self.rows()["errors"], [])

    def test_a_runaway_stack_is_clipped_rather_than_refused(self):
        """A render loop can produce a megabyte of stack. Keep the report,
        bound the row."""
        self.report(message="x" * 5000, stack="y" * 50000)
        row = self.rows()["errors"][0]
        self.assertLessEqual(len(row["message"]), 1000)
        self.assertLessEqual(len(row["stack"]), 4000)


class TestTheSameBugIsOneRow(ClientErrorTestCase):
    """`lferr.js` suppresses a repeat within one page load — not across loads,
    and not across devices. Eight tills in a render loop are one bug, and if
    each hit inserted a row the page would be unreadable by lunchtime."""

    def test_a_repeat_bumps_the_count_instead_of_adding_a_row(self):
        first = self.report(message="boom", source="/pos.js", line=9)
        again = self.report(message="boom", source="/pos.js", line=9)
        self.assertFalse(first["repeat"])
        self.assertTrue(again["repeat"])
        rows = self.rows()["errors"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hits"], 2)

    def test_the_same_fault_on_another_device_is_still_one_row(self):
        self.report(message="boom", ua="Android 11", screen="1280x728")
        self.report(message="boom", ua="iPhone", screen="390x844")
        self.assertEqual(len(self.rows()["errors"]), 1)

    def test_a_different_line_is_a_different_bug(self):
        self.report(message="boom", source="/pos.js", line=9)
        self.report(message="boom", source="/pos.js", line=88)
        self.assertEqual(len(self.rows()["errors"]), 2)

    def test_the_same_message_from_another_app_is_a_different_bug(self):
        self.report(message="boom")
        self.report(message="boom", app=self.APP + "-2")
        self.addCleanup(lambda: [
            frappe.delete_doc(DOCTYPE, n, force=True, ignore_permissions=True)
            for n in frappe.get_all(DOCTYPE, filters={"app": self.APP + "-2"}, pluck="name")])
        self.assertEqual(len(self.rows()["errors"]), 1)

    def test_a_repeat_does_not_reopen_something_already_fixed(self):
        """A manager who marked it fixed decided that. One more stray hit from a
        tab nobody reloaded must not put it back at the top of their list."""
        name = self.report(message="boom")["name"]
        api.resolve_client_error(name=name, status="resolved")
        self.report(message="boom")
        self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "resolved")
        self.assertEqual(frappe.db.get_value(DOCTYPE, name, "hits"), 2)

    def test_last_seen_moves_but_first_seen_does_not(self):
        """"Started at 6pm, still happening" is the sentence a manager needs."""
        name = self.report(message="boom")["name"]
        first = frappe.db.get_value(DOCTYPE, name, "first_seen")
        frappe.db.set_value(DOCTYPE, name, "last_seen", "2020-01-01 00:00:00")
        self.report(message="boom")
        self.assertEqual(frappe.db.get_value(DOCTYPE, name, "first_seen"), first)
        self.assertGreater(str(frappe.db.get_value(DOCTYPE, name, "last_seen")),
                           "2020-01-01 00:00:00")


class TestTheWriterNeverThrows(ClientErrorTestCase):
    """The caller is a page that has already broken. A logger that raises turns
    a bad render into a white screen at the counter."""

    def test_it_survives_whatever_the_payload_is(self):
        for payload in ({"line": "not a number"}, {"col": {}}, {"kind": None},
                        {"app": None}, {"stack": 12345}, {"screen": object()}):
            with self.subTest(payload=payload):
                self.report(message="boom", **payload)

    def test_it_reports_failure_rather_than_raising_when_the_table_is_gone(self):
        """A site mid-migration, or one where the DocType was deleted by hand."""
        original = errors._ensure_client_error_doctype
        errors._ensure_client_error_doctype = lambda: (_ for _ in ()).throw(RuntimeError("no table"))
        self.addCleanup(setattr, errors, "_ensure_client_error_doctype", original)
        self.assertEqual(self.report(message="boom"), {"ok": False})


class TestReadingTheLog(ClientErrorTestCase):
    def test_the_badge_counts_only_what_is_open(self):
        """/admin asks with limit=1 and reads open_count alone."""
        a = self.report(message="one")["name"]
        self.report(message="two")
        api.resolve_client_error(name=a, status="resolved")
        result = api.get_client_errors(status="open", limit=1)
        self.assertGreaterEqual(result["open_count"], 1)
        self.assertNotIn(a, [r["name"] for r in result["errors"]])

    def test_filtering_by_status(self):
        a = self.report(message="one")["name"]
        self.report(message="two")
        api.resolve_client_error(name=a, status="resolved")
        self.assertEqual([r["message"] for r in self.rows(status="resolved")["errors"]], ["one"])
        self.assertEqual([r["message"] for r in self.rows(status="open")["errors"]], ["two"])

    def test_no_status_means_every_row(self):
        a = self.report(message="one")["name"]
        self.report(message="two")
        api.resolve_client_error(name=a, status="resolved")
        self.assertEqual(len(self.rows(status="")["errors"]), 2)

    def test_the_app_chips_list_every_app_that_ever_reported(self):
        """Filtered to one app, the chips still have to offer the way back out."""
        self.report(message="one")
        self.report(message="two", app=self.APP + "-2")
        self.addCleanup(lambda: [
            frappe.delete_doc(DOCTYPE, n, force=True, ignore_permissions=True)
            for n in frappe.get_all(DOCTYPE, filters={"app": self.APP + "-2"}, pluck="name")])
        result = self.rows()
        self.assertEqual(len(result["errors"]), 1, "filtered to one app")
        self.assertIn(self.APP + "-2", result["apps"], "but the other app's chip survives")

    def test_the_newest_thing_is_at_the_top(self):
        old = self.report(message="old")["name"]
        self.report(message="new")
        frappe.db.set_value(DOCTYPE, old, "last_seen", "2020-01-01 00:00:00")
        self.assertEqual(self.rows()["errors"][0]["message"], "new")

    def test_an_empty_log_answers_the_shape_the_page_expects(self):
        result = self.rows()
        self.assertEqual(result["errors"], [])
        self.assertIsInstance(result["apps"], list)
        self.assertIsInstance(result["open_count"], int)


class TestActingOnAnError(ClientErrorTestCase):
    def test_marking_it_fixed_records_who_and_when(self):
        name = self.report(message="boom")["name"]
        self.assertTrue(api.resolve_client_error(name=name, status="resolved")["ok"])
        self.assertEqual(frappe.db.get_value(DOCTYPE, name, "resolved_by"), MANAGER)
        self.assertTrue(frappe.db.get_value(DOCTYPE, name, "resolved_at"))

    def test_reopening_clears_the_signature(self):
        name = self.report(message="boom")["name"]
        api.resolve_client_error(name=name, status="resolved")
        api.resolve_client_error(name=name, status="open")
        self.assertEqual(frappe.db.get_value(DOCTYPE, name, "status"), "open")
        self.assertFalse(frappe.db.get_value(DOCTYPE, name, "resolved_by"))

    def test_an_unknown_status_is_refused(self):
        name = self.report(message="boom")["name"]
        with self.assertRaises(frappe.ValidationError):
            api.resolve_client_error(name=name, status="deleted")

    def test_a_row_that_is_gone_is_not_an_error(self):
        """Two managers on /errors, both with a 30-second refresh."""
        self.assertFalse(api.resolve_client_error(name="does-not-exist")["ok"])

    def test_clearing_removes_the_fixed_rows_and_leaves_the_rest(self):
        a = self.report(message="one")["name"]
        self.report(message="two")
        api.resolve_client_error(name=a, status="resolved")
        result = api.clear_client_errors(scope="resolved")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["deleted"], 1)
        self.assertEqual([r["message"] for r in self.rows(status="")["errors"]], ["two"])

    def test_clearing_anything_but_the_fixed_rows_is_refused(self):
        """The page only ever sends "resolved". Validating it means a future
        "all" cannot be reached by guessing before anyone decided it exists."""
        self.report(message="boom")
        with self.assertRaises(frappe.ValidationError):
            api.clear_client_errors(scope="all")
        self.assertEqual(len(self.rows()["errors"]), 1)


class TestWhoMayReadIt(ClientErrorTestCase):
    """A row carries a stack trace, the page a staffer was on, their user id and
    their device string. That is a staff-surveillance surface as much as a
    debugging one — /errors gates on a manager, and so must the endpoints, or
    the gate is decoration."""

    def test_a_cashier_may_report_an_error(self):
        self.as_user(CASHIER)
        self.assertTrue(self.report(message="boom")["ok"])

    def test_a_cashier_may_not_read_the_log(self):
        self.report(message="boom")
        self.as_user(CASHIER)
        with self.assertRaises(frappe.PermissionError):
            api.get_client_errors()

    def test_a_cashier_may_not_resolve_or_clear(self):
        name = self.report(message="boom")["name"]
        self.as_user(CASHIER)
        with self.assertRaises(frappe.PermissionError):
            api.resolve_client_error(name=name, status="resolved")
        with self.assertRaises(frappe.PermissionError):
            api.clear_client_errors()

    def test_a_guest_cannot_report_at_all(self):
        """Every page that loads lferr.js is behind a staff login. An open
        endpoint that writes a row per request is a way to fill a disk."""
        self.assertNotIn("log_client_error", [
            f.__name__ for f in frappe.whitelisted
            if getattr(f, "__name__", "") == "log_client_error"
            and getattr(f, "allow_guest", False)])
        self.assertFalse(getattr(api.log_client_error, "allow_guest", False))


class TestKeepingTheTableSmall(ClientErrorTestCase):
    def test_old_resolved_rows_go_and_recent_ones_stay(self):
        old = self.report(message="old")["name"]
        recent = self.report(message="recent")["name"]
        for name in (old, recent):
            api.resolve_client_error(name=name, status="resolved")
        frappe.db.set_value(DOCTYPE, old, "last_seen",
                            frappe.utils.add_days(frappe.utils.now_datetime(),
                                                  -errors.CLIENT_ERROR_KEEP_RESOLVED_DAYS - 5))
        api.purge_client_errors()
        remaining = [r["message"] for r in self.rows(status="")["errors"]]
        self.assertEqual(remaining, ["recent"])

    def test_an_open_row_is_kept_far_longer_than_a_fixed_one(self):
        """An old open row is either still broken or was never looked at, and
        both are worth seeing."""
        self.assertGreater(errors.CLIENT_ERROR_KEEP_OPEN_DAYS,
                           errors.CLIENT_ERROR_KEEP_RESOLVED_DAYS)
        name = self.report(message="old but open")["name"]
        frappe.db.set_value(DOCTYPE, name, "last_seen",
                            frappe.utils.add_days(frappe.utils.now_datetime(),
                                                  -errors.CLIENT_ERROR_KEEP_RESOLVED_DAYS - 5))
        api.purge_client_errors()
        self.assertTrue(frappe.db.exists(DOCTYPE, name))


class TestThePagesCanReachIt(ClientErrorTestCase):
    """The bug this whole module fixes was a name mismatch, not a logic error:
    lferr.js posted to a path nobody had written. These assert the strings in
    the pages resolve."""

    def test_every_path_the_pages_call_resolves(self):
        for name in ("log_client_error", "get_client_errors",
                     "resolve_client_error", "clear_client_errors"):
            with self.subTest(endpoint=name):
                fn = frappe.get_attr(f"luuvcrm.api.{name}")
                frappe.is_whitelisted(fn)

    def test_the_reporter_still_points_at_the_endpoint_that_exists(self):
        js = frappe.read_file(frappe.get_app_path("luuvcrm", "www", "lferr.js"))
        self.assertIn("/api/method/luuvcrm.api.log_client_error", js)
