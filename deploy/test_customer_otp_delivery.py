"""Offline OTP-delivery regressions using the actual customer endpoint functions.

All Frappe records, Redis entries and SMS transport are in-memory substitutes.
No network, live site, SMS, or repository source is changed by this script.
Use --ref COMMIT to demonstrate the previous promo-verification bypass.
"""

import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


PARSER = argparse.ArgumentParser()
PARSER.add_argument("--ref")
ARGS, TEST_ARGS = PARSER.parse_known_args()
REPO = Path(__file__).resolve().parents[1]


def source(relative):
    if ARGS.ref:
        return subprocess.check_output([
            "git", "-c", "safe.directory=" + REPO.as_posix(), "-C", str(REPO),
            "show", ARGS.ref + ":" + relative,
        ]).decode("utf-8")
    return (REPO / relative).read_text(encoding="utf-8-sig")


def load_functions(relative, names, context):
    parsed = ast.parse(source(relative), filename=relative)
    nodes = []
    for node in parsed.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets):
            nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), relative, "exec"), context)
    assert all(name in context for name in names), "Requested real function/alias was not found"


class MemoryCache:
    def __init__(self):
        self.now = 0
        self.entries = {}

    def get_value(self, key, expires=False):
        assert expires, "OTP cache reads must check Redis expiry"
        value, deadline = self.entries.get(key, (None, 0))
        return value if deadline > self.now else None

    def set_value(self, key, value, expires_in_sec):
        self.entries[key] = (value, self.now + expires_in_sec)

    def delete_value(self, key):
        self.entries.pop(key, None)


class OtpDeliveryTests(unittest.TestCase):
    MOBILE = "0770000212"

    def setUp(self):
        self.promo_status = None
        self.cache = MemoryCache()
        self.transport = Mock(return_value=True)
        self.configured = Mock(return_value=True)
        self.form = {"mobile": self.MOBILE}
        self.frappe = SimpleNamespace(
            local=SimpleNamespace(form_dict=self.form),
            db=SimpleNamespace(exists=self.exists),
            cache=lambda: self.cache,
        )
        self.context = {
            "frappe": self.frappe,
            "sms": SimpleNamespace(textlk_configured=self.configured, send_textlk_sms=self.transport),
            "settings": SimpleNamespace(get=lambda key: "Luuv test restaurant"),
            "secrets": SimpleNamespace(choice=lambda alphabet: "4"),
        }
        load_functions("luuvcrm/api/wa.py", {"_wa_number"}, self.context)
        load_functions("luuvcrm/api/customers.py", {
            "_send_customer_otp", "sms_send_otp", "sms_verify_otp",
            "wa_send_otp", "wa_verify_otp", "customer_create_or_login",
        }, self.context)

    def exists(self, doctype, filters):
        if doctype == "DocType":
            return filters == "QR Promo Code"
        if doctype == "QR Promo Code":
            return (filters.get("mobile") == self.MOBILE
                    and self.promo_status in filters["status"][1])
        return False

    def call(self, name, **values):
        self.form.clear()
        self.form.update(mobile=self.MOBILE, **values)
        return self.context[name]()

    def cached(self, prefix):
        return self.cache.get_value(prefix + ":" + self.MOBILE, expires=True)

    def assert_sms_submitted(self, result):
        self.assertTrue(result.get("sent"), result)
        self.assertEqual(result["channel"], "sms")
        self.assertFalse(result.get("verified", False))
        self.transport.assert_called_once()
        recipient, message = self.transport.call_args.args
        self.assertEqual(recipient, "94" + self.MOBILE[1:])
        code = re.search(r"\b(\d{4})\b", message).group(1)
        self.assertEqual(self.cached("sms_otp"), code)
        self.assertIsNone(self.cached("sms_verified"))

    def test_registered_promo_still_receives_sms(self):
        self.promo_status = "Registered"
        self.assert_sms_submitted(self.call("sms_send_otp"))

    def test_redeemed_promo_still_receives_sms(self):
        self.promo_status = "Redeemed"
        self.assert_sms_submitted(self.call("sms_send_otp"))

    def test_phone_without_promo_keeps_normal_sms_delivery(self):
        self.assert_sms_submitted(self.call("sms_send_otp"))

    def test_promo_customer_cooldown_does_not_send_duplicate(self):
        self.promo_status = "Registered"
        self.assert_sms_submitted(self.call("sms_send_otp"))
        first_code = self.cached("sms_otp")
        result = self.call("sms_send_otp")
        self.assertTrue(result["sent"])
        self.assertTrue(result["cooldown"])
        self.transport.assert_called_once()
        self.assertEqual(self.cached("sms_otp"), first_code)
        self.assertIsNone(self.cached("sms_verified"))

    def test_failed_sms_does_not_mark_promo_customer_verified(self):
        for status in ("Registered", "Redeemed"):
            with self.subTest(promo_status=status):
                self.promo_status = status
                self.transport.reset_mock()
                self.transport.return_value = False
                result = self.call("sms_send_otp")
                self.assertFalse(result.get("sent"))
                self.assertFalse(result.get("verified", False))
                self.assertIn("error", result)
                self.transport.assert_called_once()
                for prefix in ("sms_verified", "sms_otp", "sms_otp_channel", "sms_otp_cool"):
                    self.assertIsNone(self.cached(prefix))

    def test_unconfigured_sms_cannot_use_promo_as_verification(self):
        self.promo_status = "Redeemed"
        self.configured.return_value = False
        result = self.call("sms_send_otp")
        self.assertFalse(result.get("sent"))
        self.assertEqual(result.get("error"), "SMS service is not configured")
        self.transport.assert_not_called()
        self.assertIsNone(self.cached("sms_verified"))

    def test_promo_phone_is_verified_only_after_correct_sms_code(self):
        self.promo_status = "Registered"
        self.assert_sms_submitted(self.call("sms_send_otp"))
        code = self.cached("sms_otp")
        self.assertFalse(self.call("sms_verify_otp", code="9999")["ok"])
        self.assertIsNone(self.cached("sms_verified"))
        self.assertTrue(self.call("sms_verify_otp", code=code)["ok"])
        self.assertEqual(self.cached("sms_verified"), 1)
        self.assertIsNone(self.cached("sms_otp"))

    def test_legacy_whatsapp_named_endpoint_also_sends_sms(self):
        self.promo_status = "Redeemed"
        self.assertIs(self.context["wa_send_otp"], self.context["sms_send_otp"])
        self.assert_sms_submitted(self.call("wa_send_otp"))

    def test_existing_account_keeps_pin_login_without_requesting_sms(self):
        self.promo_status = "Registered"
        self.frappe.db.get_value = Mock(return_value="EXISTING-ACCOUNT")
        self.frappe.get_doc = Mock(return_value=SimpleNamespace(customer_name="Existing customer"))
        result = self.call("customer_create_or_login")
        self.assertTrue(result["success"])
        self.assertTrue(result["exists"])
        self.transport.assert_not_called()
        self.assertIsNone(self.cached("sms_verified"))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *TEST_ARGS], verbosity=2)
