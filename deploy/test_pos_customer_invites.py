"""Actual-source POS/customer invitation boundary tests, without Frappe or SMS.

Document, transaction, and queue doubles verify decisions and durable-state
ordering. Real database lock semantics and Text.lk delivery are not simulated.
"""
import ast
from copy import deepcopy
from datetime import datetime
from functools import partial
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1] / "luuvcrm"
MOBILE = "0771234567"
CONTACT = "Customer App Contact"


class ValidationError(Exception):
    pass


class MissingDocument(Exception):
    pass


class Record(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)

    def __contains__(self, key):
        return hasattr(self, key)

    def __getitem__(self, key):
        return getattr(self, key)

    def update(self, values):
        vars(self).update(values)


class Database:
    def __init__(self, h):
        self.h = h
        self.after_commit = SimpleNamespace(add=Mock(side_effect=h.callbacks.append))
        self.savepoint = Mock(side_effect=self._savepoint)
        self.rollback = Mock(side_effect=self._rollback)
        self.commit = Mock(side_effect=self._commit)
        self.set_value = Mock(side_effect=self._set_value)
        self.sql = Mock(side_effect=self._sql)
        self.has_column = Mock(return_value=True)
        self.snapshots = {}

    def exists(self, doctype, key):
        if doctype == "DocType":
            return key in self.h.schema
        if doctype == "Customer Push Token":
            return any(token.mobile in key["mobile"][1]
                       and token.platform in key["platform"][1] for token in self.h.tokens)
        return key in self.h.rows.get(doctype, {})

    def get_value(self, doctype, key, fields, **kwargs):
        return self.h.rows.get(doctype, {}).get(key)

    def _sql(self, query, values):
        if "INSERT IGNORE INTO `tabCustomer App Contact`" not in query:
            raise AssertionError("Unexpected SQL in invitation test: " + query)
        mobile = values[0]
        self.h.rows[CONTACT].setdefault(mobile, Record(name=mobile, mobile=mobile, app_installed=0))
        self.h.events.append(("contact_insert_if_absent", mobile))

    def _set_value(self, doctype, name, values, value=None):
        if isinstance(values, str):
            values = {values: value}
        self.h.rows[doctype][name].update(values)
        self.h.events.append(("write", doctype, name, dict(values)))

    def _savepoint(self, name):
        self.snapshots[name] = deepcopy(self.h.rows)

    def _rollback(self, save_point=None):
        if save_point in self.snapshots:
            self.h.rows = self.snapshots[save_point]
        self.h.events.append(("rollback", save_point))

    def _commit(self):
        self.h.events.append(("commit",))
        callbacks, self.h.callbacks[:] = self.h.callbacks[:], []
        for callback in callbacks:
            callback()


class Harness:
    def __init__(self):
        self.rows = {CONTACT: {}, "POS Order": {}, "POS Invoice": {}, "Sales Invoice": {}}
        self.schema = {CONTACT, "Customer Push Token"}
        self.tokens = []
        self.callbacks = []
        self.events = []
        self.reads = []
        self.list_calls = []
        self.db = Database(self)
        self.require_pos = Mock()
        self.require_access = Mock(return_value="Kurunegala")
        self.enqueue = Mock()
        self.log_error = Mock()
        self.configured = Mock(return_value=True)
        self.send = Mock(side_effect=self._send)
        self.resolve_token = Mock(return_value=MOBILE)
        self.branch = SimpleNamespace(FIELD="restaurant", require_access=self.require_access,
            viewing=Mock(return_value="Kurunegala"),
            available_restaurants=Mock(return_value=["Kurunegala"]))
        self.frappe = SimpleNamespace(local=SimpleNamespace(form_dict=Record()),
            session=SimpleNamespace(user="test-cashier"), conf={}, db=self.db,
            get_doc=self.get_doc, get_list=self.get_list, get_all=self.get_all, enqueue=self.enqueue,
            log_error=self.log_error, _dict=lambda **values: Record(**values),
            throw=self.throw, PermissionError=ValidationError, DoesNotExistError=MissingDocument)
        phones = {}
        exec(compile((APP / "customer_phone.py").read_text(encoding="utf-8"),
                     str(APP / "customer_phone.py"), "exec"), phones)
        self.ns = dict(frappe=self.frappe, _=lambda text: text, partial=partial,
            now_datetime=lambda: datetime(2026, 11, 14, 12), branch=self.branch,
            roles=SimpleNamespace(require_pos=self.require_pos),
            settings=SimpleNamespace(get=lambda key: {"public_url": "https://luuvgrand.com"}.get(key)),
            normalize_mobile=phones["normalize_mobile"], phone_variants=phones["phone_variants"])
        tree = ast.parse((APP / "api/pos_customers.py").read_text(encoding="utf-8"))
        definitions = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
                definitions.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                definitions.append(node)
        exec(compile(ast.fix_missing_locations(ast.Module(body=definitions, type_ignores=[])),
                     str(APP / "api/pos_customers.py"), "exec"), self.ns)
        self.modules = {}
        for name, values in {
            "luuvcrm.api.sms": dict(textlk_configured=self.configured, send_textlk_sms=self.send),
            "luuvcrm.api.shifts": dict(_get_invoice_doc=self.get_invoice),
            "luuvcrm.api.customers": dict(_mobile_from_token=self.resolve_token),
            "luuvcrm.api.orders": dict(lines_by_order=lambda names: {}),
            "luuvcrm.api.pos_inventory": dict(list_filter=lambda: {"inventory_mode": 0}),
        }.items():
            module = ModuleType(name)
            vars(module).update(values)
            self.modules[name] = module

    @staticmethod
    def throw(message, *args):
        raise ValidationError(message)

    def get_doc(self, doctype, name, **kwargs):
        self.reads.append((doctype, name, kwargs))
        if name not in self.rows[doctype]:
            raise MissingDocument(name)
        return self.rows[doctype][name]

    def get_invoice(self, name):
        for doctype in ("POS Invoice", "Sales Invoice"):
            if name in self.rows[doctype]:
                return self.rows[doctype][name]
        raise MissingDocument(name)

    def get_list(self, doctype, **kwargs):
        self.list_calls.append((doctype, kwargs))
        candidates = [row for row in self.rows[doctype].values()
            if row.mobile in kwargs["filters"]["mobile"][1]
            and row.restaurant == kwargs["filters"]["restaurant"]
            and row.pos_invoice and row.docstatus != 2 and row.kitchen_status != "Cancelled"]
        limit = kwargs["limit_page_length"]
        return candidates[:limit] if limit else candidates

    def get_all(self, doctype, **kwargs):
        filters = kwargs["filters"]
        if doctype not in {"POS Invoice", "Sales Invoice"} or kwargs["pluck"] != "name":
            raise AssertionError("Unexpected unpermissioned read in invitation test")
        return [row.name for row in self.rows[doctype].values()
            if row.name in filters["name"][1] and row.docstatus == filters["docstatus"]
            and row.is_return == filters["is_return"]]

    def _send(self, recipient, message):
        contact = self.rows[CONTACT][MOBILE]
        if contact.invite_status != "Sending" or self.events[-1] != ("commit",):
            raise AssertionError("Provider called before durable dispatch claim")
        return True

    def order(self, name="ORDER-1", **values):
        defaults = dict(name=name, mobile=MOBILE, customer_app_invite=1, order_source="Walk-in",
            kitchen_status="Served", docstatus=1, pos_invoice="INV-"+name,
            waiter_name="Test Cashier", items=[Record(paid_qty=0)], restaurant="Kurunegala",
            grand_total=1000, customer_name="Test Customer", creation="2026-11-14 10:00:00")
        defaults.update(values)
        order = Record(**defaults)
        self.rows["POS Order"][name] = order
        if order.pos_invoice:
            self.rows["POS Invoice"][order.pos_invoice] = Record(name=order.pos_invoice, docstatus=1, is_return=0)
        return order

    def run(self, function, *args, form=None, **kwargs):
        if form is not None:
            self.frappe.local.form_dict = Record(**form)
        with patch.dict(sys.modules, self.modules):
            return self.ns[function](*args, **kwargs)

    def queue(self, order=None):
        order = order or self.order()
        assert self.run("queue_install_invite", order.name)
        with patch.dict(sys.modules, self.modules):
            self.db.commit()
        return order


class PhoneAndInstallationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_aliases_normalize_to_one_durable_key(self):
        for value in [MOBILE, "94771234567", "+94771234567", "771234567", "+94 (77) 123-4567", "077 123 4567"]:
            with self.subTest(value=value):
                self.assertEqual(self.h.run("_mobile", value), MOBILE)
        self.assertEqual(set(self.h.ns["phone_variants"](MOBILE)),
                         {MOBILE, "94771234567", "+94771234567", "771234567"})

    def test_invalid_numbers_do_not_become_sms_recipients(self):
        for value in ["", "abc0771234567", "0111234567", "07712345678", "770"]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.h.run("_mobile", value)

    def test_disabled_native_tokens_still_suppress_invites(self):
        self.h.tokens.append(Record(mobile="94771234567", platform="Android", enabled=0))
        self.assertTrue(self.h.run("_known_installed", MOBILE))

    def test_web_push_token_alone_does_not_claim_pwa_installation(self):
        self.h.tokens.append(Record(mobile=MOBILE, platform="Web", enabled=1))
        self.assertFalse(self.h.run("_known_installed", MOBILE))
        self.h.run("_mark_installed", MOBILE, "PWA")
        self.assertTrue(self.h.run("_known_installed", MOBILE))

    def test_install_record_requires_valid_customer_session_and_supported_platform(self):
        self.h.resolve_token.return_value = ""
        self.assertFalse(self.h.run("customer_record_app_install", customer_token="bad", platform="Android")["success"])
        self.h.resolve_token.return_value = MOBILE
        self.assertFalse(self.h.run("customer_record_app_install", customer_token="good", platform="Staff")["success"])
        self.assertEqual(self.h.rows[CONTACT], {})
        self.assertTrue(self.h.run("customer_record_app_install", customer_token="good", platform="PWA")["success"])
        self.assertEqual(self.h.rows[CONTACT][MOBILE].app_platform, "PWA")

    def test_install_record_uses_authenticated_phone_instead_of_submitted_phone(self):
        result = self.h.run("customer_record_app_install", form={
            "customer_token": "valid-session", "platform": "Android", "mobile": "0770000001"})
        self.assertTrue(result["success"])
        self.assertEqual(list(self.h.rows[CONTACT]), [MOBILE])
        self.h.resolve_token.assert_called_once_with("valid-session")


class MetadataAndAccessTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_no_pos_flag_leaves_unrelated_payload_untouched(self):
        self.assertEqual(self.h.run("prepare_pos_customer", {"mobile": "legacy"}), {})
        self.h.require_pos.assert_not_called()

    def test_prepare_requires_pos_role_and_normalizes_invite(self):
        result = self.h.run("prepare_pos_customer", {"pos_customer": 1, "mobile": "+94771234567", "customer_app_action": "invite"})
        self.assertEqual(result, {"mobile": MOBILE, "customer_app_invite": 1})
        self.h.require_pos.assert_called_once()
        self.h.require_pos.side_effect = ValidationError("Forbidden")
        with self.assertRaises(ValidationError):
            self.h.run("prepare_pos_customer", {"pos_customer": 1, "mobile": MOBILE})

    def test_staff_installed_evidence_cancels_queued_message(self):
        self.h.queue()
        result = self.h.run("prepare_pos_customer", {"pos_customer": 1, "mobile": MOBILE, "customer_app_action": "installed"})
        self.assertEqual(result["customer_app_invite"], 0)
        contact = self.h.rows[CONTACT][MOBILE]
        self.assertEqual(contact.app_platform, "Staff")
        self.assertEqual(contact.invite_status, "Skipped")
        self.h.send.assert_not_called()

    def test_order_metadata_rejects_paid_cancelled_or_partly_paid(self):
        for overrides in [dict(docstatus=1), dict(pos_invoice="ALREADY-PAID"),
                          dict(kitchen_status="Cancelled"), dict(items=[Record(paid_qty=1)])]:
            with self.subTest(overrides=overrides):
                h = Harness()
                order = h.order(docstatus=0, pos_invoice="", kitchen_status="Pending", **{
                    key: value for key, value in overrides.items() if key not in {"docstatus", "pos_invoice", "kitchen_status"}})
                order.update(overrides)
                with self.assertRaises(ValidationError):
                    h.run("pos_set_order_customer", form={"order_name": order.name, "mobile": "0770000001"})
                self.assertEqual(order.mobile, MOBILE)
                h.db.set_value.assert_not_called()

    def test_bound_customer_identity_cannot_be_reassigned(self):
        cases = [dict(waiter_name="CUS"), dict(waiter_name=" CUS "),
                 dict(reward_redemption_request="REWARD-1"), dict(customer_offer="OFFER-1"),
                 dict(reward_redeem_amount=100), dict(offer_discount_amount=100)]
        for bound in cases:
            with self.subTest(bound=bound):
                h = Harness()
                order = h.order(docstatus=0, pos_invoice="", kitchen_status="Pending", **bound)
                with self.assertRaises(ValidationError):
                    h.run("pos_set_order_customer", form={"order_name": order.name, "mobile": "0770000001"})
                self.assertEqual(order.mobile, MOBILE)

    def test_order_customer_edit_locks_then_persists_normalized_phone(self):
        order = self.h.order(docstatus=0, pos_invoice="", kitchen_status="Pending")
        result = self.h.run("pos_set_order_customer", form={"order_name": order.name, "mobile": "+94771234567", "app_action": "invite"})
        self.assertTrue(result["success"])
        self.assertEqual(order.mobile, MOBILE)
        self.assertEqual(order.customer_app_invite, 1)
        self.assertEqual(self.h.reads[0], ("POS Order", order.name, {"for_update": True}))
        self.h.require_access.assert_called_once_with("POS Order", order.name)

    def test_wrong_branch_refuses_before_reading_or_changing_order(self):
        self.h.require_access.side_effect = ValidationError("Wrong branch")
        with self.assertRaises(ValidationError):
            self.h.run("pos_set_order_customer", form={"order_name": "ORDER-1", "mobile": MOBILE})
        self.assertEqual(self.h.reads, [])
        self.h.db.set_value.assert_not_called()

    def test_accessible_order_in_another_current_branch_is_still_rejected(self):
        self.h.require_access.return_value = "Luuv Fryxo"
        with self.assertRaisesRegex(ValidationError, "another restaurant"):
            self.h.run("pos_set_order_customer", form={"order_name": "ORDER-1", "mobile": MOBILE})
        self.assertEqual(self.h.reads, [])
        self.h.db.set_value.assert_not_called()

    def test_all_staff_endpoints_reject_missing_pos_authority_before_data_access(self):
        for name in ["prepare_pos_customer", "pos_set_order_customer", "pos_customer_history"]:
            with self.subTest(endpoint=name):
                h = Harness()
                h.require_pos.side_effect = ValidationError("POS permission required")
                args = ({"pos_customer": 1, "mobile": MOBILE},) if name == "prepare_pos_customer" else ()
                with self.assertRaisesRegex(ValidationError, "POS permission required"):
                    h.run(name, *args, form={"order_name": "ORDER-1", "mobile": MOBILE})
                self.assertEqual(h.reads, [])
                self.assertEqual(h.list_calls, [])
                h.db.set_value.assert_not_called()

    def test_history_is_pos_authorized_and_filtered_to_current_branch(self):
        self.h.order()
        self.h.order(name="OTHER-BRANCH", restaurant="Luuv Fryxo")
        result = self.h.run("pos_customer_history", form={"mobile": "+94771234567"})
        self.assertEqual(result["purchase_count"], 1)
        self.assertEqual([row["name"] for row in result["purchases"]], ["ORDER-1"])
        self.assertTrue(self.h.require_pos.called)
        for _, options in self.h.list_calls:
            self.assertEqual(options["filters"]["restaurant"], "Kurunegala")
            self.assertEqual(options["filters"]["inventory_mode"], 0)
        self.h.branch.available_restaurants.return_value = ["Luuv Fryxo"]
        with self.assertRaises(ValidationError):
            self.h.run("pos_customer_history", form={"mobile": MOBILE})

    def test_history_totals_include_only_submitted_non_return_sales(self):
        self.h.order(name="PAID-POS", grand_total=1200)
        sales_order = self.h.order(name="PAID-SALES", grand_total=800)
        self.h.rows["Sales Invoice"][sales_order.pos_invoice] = self.h.rows["POS Invoice"].pop(sales_order.pos_invoice)
        for state in ["draft", "cancelled", "return", "missing"]:
            order = self.h.order(name="INVALID-"+state, grand_total=9000)
            invoice = self.h.rows["POS Invoice"][order.pos_invoice]
            if state == "missing":
                del self.h.rows["POS Invoice"][order.pos_invoice]
            elif state == "return":
                invoice.is_return = 1
            else:
                invoice.docstatus = 0 if state == "draft" else 2
        self.h.order(name="UNPAID", docstatus=0, pos_invoice="", grand_total=9000)
        self.h.order(name="CANCELLED-ORDER", docstatus=2, grand_total=9000)
        result = self.h.run("pos_customer_history", form={"mobile": MOBILE})
        self.assertEqual(result["purchase_count"], 2)
        self.assertEqual(result["purchase_total"], 2000)
        self.assertEqual({row["name"] for row in result["purchases"]}, {"PAID-POS", "PAID-SALES"})


class InvitationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_queue_is_after_commit_and_duplicate_purchases_share_one_claim(self):
        first = self.h.order()
        other = self.h.order(name="ORDER-2", mobile="+94771234567", restaurant="Luuv Fryxo")
        self.assertTrue(self.h.run("queue_install_invite", first.name))
        self.assertFalse(self.h.run("queue_install_invite", other.name))
        self.assertEqual(len(self.h.callbacks), 1)
        self.h.enqueue.assert_not_called()
        self.h.send.assert_not_called()
        self.assertIn((CONTACT, MOBILE, {"for_update": True}), self.h.reads)
        with patch.dict(sys.modules, self.h.modules):
            self.h.db.commit()
        self.h.enqueue.assert_called_once()
        self.assertEqual(self.h.enqueue.call_args.kwargs["mobile"], MOBILE)
        self.assertEqual(self.h.enqueue.call_args.kwargs["order_name"], first.name)
        self.h.send.assert_not_called()

    def test_ineligible_order_or_missing_provider_does_not_queue(self):
        for overrides in [dict(customer_app_invite=0), dict(mobile="bad"),
                          dict(order_source="Online"), dict(pos_invoice=""),
                          dict(docstatus=2), dict(kitchen_status="Cancelled")]:
            with self.subTest(overrides=overrides):
                h = Harness()
                order = h.order(**overrides)
                self.assertFalse(h.run("queue_install_invite", order.name))
                self.assertEqual(h.callbacks, [])
                h.send.assert_not_called()
        order = self.h.order()
        self.h.configured.return_value = False
        self.assertFalse(self.h.run("queue_install_invite", order.name))

    def test_legacy_direct_paid_sale_is_eligible_without_completed_order_status(self):
        order = self.h.order(docstatus=0, kitchen_status="Pending")
        self.assertTrue(self.h.run("queue_install_invite", order.name))

    def test_queue_known_native_install_is_suppressed(self):
        order = self.h.order()
        self.h.tokens.append(Record(mobile="94771234567", platform="iOS", enabled=0))
        self.assertFalse(self.h.run("queue_install_invite", order.name))
        self.assertEqual(self.h.callbacks, [])

    def test_worker_rechecks_installation_after_queuing(self):
        order = self.h.queue()
        self.h.tokens.append(Record(mobile=MOBILE, platform="Android", enabled=1))
        result = self.h.run("send_install_invite", MOBILE, order.name)
        self.assertFalse(result["sent"])
        self.assertEqual(self.h.rows[CONTACT][MOBILE].invite_status, "Skipped")
        self.h.send.assert_not_called()

    def test_worker_commits_claim_before_provider_and_duplicate_job_cannot_send(self):
        order = self.h.queue()
        original_send = self.h._send
        duplicate_results = []
        def simultaneous_duplicate(recipient, message):
            original_send(recipient, message)
            duplicate_results.append(self.h.run("send_install_invite", MOBILE, order.name))
            return True
        self.h.send.side_effect = simultaneous_duplicate
        self.assertTrue(self.h.run("send_install_invite", MOBILE, order.name)["sent"])
        self.assertFalse(duplicate_results[0]["sent"])
        self.assertEqual(self.h.rows[CONTACT][MOBILE].invite_status, "Sent")
        self.h.send.assert_called_once()
        self.assertEqual(self.h.send.call_args.args[0], "94771234567")
        self.assertIn("https://luuvgrand.com/cus?install=1", self.h.send.call_args.args[1])

    def test_provider_rejection_or_ambiguous_exception_is_never_auto_retried(self):
        for failure in [False, TimeoutError("network result unknown")]:
            with self.subTest(failure=type(failure).__name__):
                h = Harness()
                order = h.queue()
                h.send.side_effect = failure if isinstance(failure, Exception) else None
                h.send.return_value = False
                self.assertFalse(h.run("send_install_invite", MOBILE, order.name)["sent"])
                self.assertEqual(h.rows[CONTACT][MOBILE].invite_status, "Failed")
                self.assertFalse(h.run("send_install_invite", MOBILE, order.name)["sent"])
                h.send.assert_called_once()
                self.assertFalse(h.run("queue_install_invite", order.name))

    def test_worker_rejects_revoked_consent_or_changed_phone(self):
        for values in [dict(customer_app_invite=0), dict(mobile="0770000001")]:
            with self.subTest(values=values):
                h = Harness()
                order = h.queue()
                order.update(values)
                self.assertFalse(h.run("send_install_invite", MOBILE, order.name)["sent"])
                self.assertEqual(h.rows[CONTACT][MOBILE].invite_status, "Skipped")
                h.send.assert_not_called()

    def test_worker_rejects_invalid_paid_invoice(self):
        for state in ["draft", "cancelled", "return", "missing"]:
            with self.subTest(state=state):
                h = Harness()
                order = h.queue()
                invoice = h.rows["POS Invoice"][order.pos_invoice]
                if state == "missing":
                    del h.rows["POS Invoice"][order.pos_invoice]
                elif state == "return":
                    invoice.is_return = 1
                else:
                    invoice.docstatus = 0 if state == "draft" else 2
                self.assertFalse(h.run("send_install_invite", MOBILE, order.name)["sent"])
                self.assertEqual(h.rows[CONTACT][MOBILE].invite_status, "Skipped")
                h.send.assert_not_called()

    def test_wrong_job_order_cannot_consume_another_invitation(self):
        self.h.queue()
        other = self.h.order(name="ORDER-2")
        self.assertFalse(self.h.run("send_install_invite", MOBILE, other.name)["sent"])
        self.assertEqual(self.h.rows[CONTACT][MOBILE].invite_status, "Queued")
        self.h.send.assert_not_called()

    def test_invalid_install_url_never_reaches_sms_provider(self):
        order = self.h.queue()
        self.h.frappe.conf["customer_app_install_url"] = "http://example.test/app"
        self.assertFalse(self.h.run("send_install_invite", MOBILE, order.name)["sent"])
        self.assertEqual(self.h.rows[CONTACT][MOBILE].invite_status, "Failed")
        self.h.send.assert_not_called()

    def test_queue_failure_is_best_effort_even_when_cleanup_and_logging_fail(self):
        order = self.h.order()
        self.h.db.savepoint.side_effect = RuntimeError("database unavailable")
        self.h.db.rollback.side_effect = RuntimeError("rollback unavailable")
        self.h.log_error.side_effect = RuntimeError("logging unavailable")
        self.assertFalse(self.h.run("queue_install_invite", order.name))
        self.h.send.assert_not_called()

    def test_after_commit_enqueue_failure_never_propagates(self):
        order = self.h.order()
        self.assertTrue(self.h.run("queue_install_invite", order.name))
        self.h.enqueue.side_effect = RuntimeError("queue unavailable")
        self.h.db.set_value.side_effect = RuntimeError("database unavailable")
        self.h.log_error.side_effect = RuntimeError("logging unavailable")
        with patch.dict(sys.modules, self.h.modules):
            self.h.db.commit()
        self.h.enqueue.assert_called_once()
        self.h.send.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
