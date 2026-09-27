"""Provisioning a site from the repo, rather than from a checklist.

`setup_doctype.py` declares everything this app adds on top of ERPNext +
Hospitality: 24 custom DocTypes, 32 custom fields, property setters, roles,
DocPerms, the branch column, and the reference rows the screens cannot work
without. `after_install` runs it, so `bench install-app luuvcrm` provisions a
site on its own instead of needing ~27 `setup_*` endpoints called by hand in an
undocumented order.

**Why it must also do what the patches do.** `frappe.installer` calls
`set_all_patches_as_completed()` on a fresh install — every patch in
`patches.txt` is marked done *without running*. So on a new site none of
`create_roles`, `grant_pos_permissions`, `grant_app_permissions` or
`add_branch_field` ever fires, and `after_install` is the only thing that does.
A fresh site and a migrated site have to end up the same, and these tests are
what says so.
"""

import json
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import branch, roles, setup_doctype as setup


class TestTheDeclarationMatchesTheSite(FrappeTestCase):
    """This site was provisioned by hand over months. The declaration is only
    worth anything if it describes what a working site actually has."""

    def test_this_site_has_no_drift(self):
        drift = setup.report_drift()
        self.assertEqual(drift["doctypes"], [], "DocTypes the declaration says are missing")
        self.assertEqual(drift["custom_fields"], [], "custom fields missing")
        self.assertEqual(drift["roles"], [], "roles missing")
        self.assertEqual(drift["branch_columns"], [], "branch columns missing")
        self.assertEqual(drift["docperms"], [], "DocPerms missing")
        self.assertEqual(drift["walkable_perms"], [],
                         "permissions that let a guard be walked around")

    def test_running_it_again_changes_nothing(self):
        """Create-only, top to bottom. Running it on a provisioned site is the
        thing somebody will do by accident on production."""
        setup.execute()
        self.assertEqual(setup.report_drift()["doctypes"], [])
        self.assertEqual(setup.report_drift()["custom_fields"], [])


class TestItCarriesThePatchesWork(FrappeTestCase):
    """Each of these is provided by a patch on a migrated site and by nothing at
    all on a fresh one, unless execute() does it."""

    def test_execute_provisions_the_roles(self):
        self.assertIn(roles.ensure_roles, _steps())

    def test_execute_provisions_the_permissions(self):
        for fn in (roles.ensure_pos_docperms, roles.ensure_app_docperms,
                   roles.revoke_app_docperms):
            with self.subTest(step=fn.__name__):
                self.assertIn(fn, _steps())

    def test_execute_provisions_the_branch_column(self):
        self.assertIn(branch.ensure_branch_field, _steps())

    def test_the_revoke_runs_after_every_grant(self):
        """Order matters: revoking first would take away what a later grant puts
        straight back, and the run would look clean either way."""
        names = [getattr(f, "__name__", "") for f in _steps()]
        self.assertLess(names.index("ensure_app_docperms"), names.index("revoke_app_docperms"))
        self.assertLess(names.index("ensure_pos_docperms"), names.index("revoke_app_docperms"))

    def test_roles_are_declared_in_one_place(self):
        """`setup_doctype` used to carry its own four-role list and its own
        DocPerm grants, which predate Restaurant Owner and contradicted
        roles.py — two sources of truth is how a Cashier ended up able to
        answer their own approval."""
        src = frappe.read_file(frappe.get_app_path("luuvcrm", "setup_doctype.py"))
        self.assertNotIn("ROLE_GRANTS", src)
        self.assertNotIn('"role": "Cashier"', src)
        self.assertNotIn('"role": "Guest"', src)


def _steps():
    """The functions execute() calls, in order, without running them."""
    called = []
    real_step = setup._step
    setup._step = lambda label, fn: called.append(fn)
    try:
        setup.execute()
    finally:
        setup._step = real_step
    return called


class TestItSurvivesBeingWrong(FrappeTestCase):
    """Provisioning half a site and reporting success is worse than failing."""

    def test_one_bad_field_does_not_cost_the_others(self):
        """`_step` wraps the whole call, so without per-field handling a single
        bad declaration rolls back every field that already succeeded and prints
        one "skipped" line for all of it. That is exactly what a missing
        `Sales Invoice.pos_order` did the first time this was ever run."""
        bad = {"dt": "POS Order", "fieldname": "_lft_bad_field", "label": "Bad",
               "fieldtype": "Link", "options": "No Such DocType At All"}
        good = {"dt": "POS Order", "fieldname": "_lft_good_field", "label": "Good",
                "fieldtype": "Data"}
        original = setup.CUSTOM_FIELDS
        setup.CUSTOM_FIELDS = [bad, good]
        self.addCleanup(setattr, setup, "CUSTOM_FIELDS", original)
        self.addCleanup(_drop_field, "POS Order", "_lft_good_field")
        self.addCleanup(_drop_field, "POS Order", "_lft_bad_field")

        setup.add_custom_fields()
        self.assertTrue(setup._has_field("POS Order", "_lft_good_field"),
                        "a later field was lost to an earlier one's failure")

    def test_a_failing_step_does_not_stop_the_run(self):
        """No savepoints: DDL implicitly commits in MariaDB and discards them,
        so rolling back to one raises and takes the rest of provisioning with
        it. A step that fails must be logged and skipped, nothing more."""
        calls = []
        real_step = setup._step
        try:
            real_step("boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            real_step("after", lambda: calls.append(True))
        finally:
            setup._step = real_step
        self.assertEqual(calls, [True], "a failed step stopped the ones after it")


def _drop_field(dt, fieldname):
    name = frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname})
    if name:
        frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
        frappe.db.commit()


class TestTheRunbookCanBeFollowed(FrappeTestCase):
    """docs/DEVELOPER_GUIDE.md §4.2 and §4.5 tell an operator to run things by
    dotted path, at the one moment nobody wants a surprise — the site is in
    maintenance mode and the restaurant is waiting. A path that has been
    renamed since the guide was written fails as `AttributeError` on a box with
    no developer on it, so the guide is worth checking like code."""

    def _guide(self):
        # get_app_path lowercases the parts it joins, so the filename is built
        # here rather than passed to it.
        docs = pathlib.Path(frappe.get_app_path("luuvcrm")).parent / "docs"
        return (docs / "DEVELOPER_GUIDE.md").read_text()

    def test_every_bench_execute_in_the_guide_resolves(self):
        import re

        paths = sorted(set(re.findall(r"execute\s+(luuvcrm[\w.]+)", self._guide())))
        self.assertTrue(paths, "the guide should tell the operator what to run")
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(callable(frappe.get_attr(path)), f"{path} is not callable")

    def test_the_runbook_says_a_new_field_needs_a_patch(self):
        """The one rule an upgrade can silently break: after_install runs on a
        new site only, so a field or setting added without a numbered patch
        works on every developer's site and never reaches production."""
        guide = self._guide()
        self.assertIn("patches.txt", guide)
        self.assertIn("CUSTOM_FIELDS", guide)


class TestEveryRuntimeFieldIsDeclared(FrappeTestCase):
    """`setup_*` endpoints still call `create_custom_field` — they are kept for
    the sites that already call them, and Phase 3 deletes them once every one
    has a patch behind it. Until then the danger is not that they run, it is
    that one of them creates a field `CUSTOM_FIELDS` has never heard of: the
    live site would have it because somebody clicked, and a fresh install
    would not, and the two would drift in the one direction nothing checks.
    """

    def _runtime_fields(self):
        import pathlib
        import re

        call = re.compile(r'create_custom_field\(\s*"([^"]+)"\s*,\s*\{(.*?)\}\s*\)', re.S)
        found = set()
        for path in sorted(pathlib.Path(frappe.get_app_path("luuvcrm")).rglob("*.py")):
            if "tests" in path.parts or "patches" in path.parts:
                continue
            for match in call.finditer(path.read_text()):
                name = re.search(r'"fieldname":\s*"(\w+)"', match.group(2))
                if name:
                    found.add((match.group(1), name.group(1)))
        return found

    def test_every_field_an_endpoint_creates_is_in_the_declaration(self):
        declared = {(f["dt"], f["fieldname"]) for f in setup.CUSTOM_FIELDS}
        runtime = self._runtime_fields()
        self.assertTrue(runtime, "the parser found no create_custom_field calls at all")
        undeclared = sorted(runtime - declared)
        self.assertEqual(undeclared, [],
                         f"created at runtime but never declared: {undeclared}")

    def test_the_only_call_the_parser_cannot_read_is_the_branch_field(self):
        """The parser above only reads calls whose doctype is a string literal.
        `branch.ensure_branch_field()` loops over a list of doctypes with the
        field in a variable, so it is invisible to that check — it has a patch
        of its own (add_branch_field) and is fine. Any *other* call built out of
        variables would slip past the declaration check unnoticed, so name them:
        one more here means the check above stopped covering the file."""
        import pathlib
        import re

        dynamic = []
        for path in sorted(pathlib.Path(frappe.get_app_path("luuvcrm")).rglob("*.py")):
            if "tests" in path.parts or "patches" in path.parts:
                continue
            for line in path.read_text().splitlines():
                call = re.search(r'create_custom_field\(\s*([^"\s)])', line)
                if call:
                    dynamic.append(path.name)
        self.assertEqual(sorted(set(dynamic)), ["branch.py"],
                         "a create_custom_field call the declaration check cannot see")

    def test_the_declaration_reaches_a_site_that_already_exists(self):
        """It did not. after_install applies CUSTOM_FIELDS, and after_install
        runs on a new site only — so on the live restaurant the fields existed
        because somebody had called a setup_* endpoint over HTTP, and a field
        added to the declaration would never have arrived. bench migrate had no
        step that looked."""
        patches = frappe.read_file(frappe.get_app_path("luuvcrm", "..", "luuvcrm", "patches.txt"))
        self.assertIn("luuvcrm.patches.v1_0.sync_custom_fields", patches)

    def test_the_patch_adds_nothing_on_a_site_that_is_already_right(self):
        from luuvcrm.patches.v1_0 import sync_custom_fields

        before = frappe.db.count("Custom Field")
        sync_custom_fields.execute()
        self.assertEqual(frappe.db.count("Custom Field"), before,
                         "the patch must be a no-op on a site that has the fields")


class TestTheHookIsWired(FrappeTestCase):
    def test_after_install_runs_it(self):
        from luuvcrm import hooks
        self.assertEqual(hooks.after_install, "luuvcrm.setup_doctype.after_install")

    def test_it_is_not_wired_to_after_migrate(self):
        """Every step is create-only, but a live deploy is the wrong place to
        find out we were wrong about that."""
        from luuvcrm import hooks
        self.assertFalse(getattr(hooks, "after_migrate", None))


def _doctype_folders():
    root = pathlib.Path(frappe.get_app_path("luuvcrm", "zeloura", "doctype"))
    return [d for d in sorted(root.iterdir())
            if d.is_dir() and (d / f"{d.name}.json").exists()]


class TestTheDocTypesShipAsFiles(FrappeTestCase):
    """B1: the schema is in the repo as DocType JSON, so `bench migrate` syncs
    it and a field change has a migration path. They used to be created at
    runtime with `custom: 1`, which kept them out of migrate's way entirely."""

    def test_every_expected_doctype_has_a_json_file(self):
        shipped = {json.loads((d / f"{d.name}.json").read_text())["name"]
                   for d in _doctype_folders()}
        missing = [n for n in setup.EXPECTED_DOCTYPES if n not in shipped]
        self.assertEqual(missing, [], "declared but not shipped as JSON")

    def test_none_of_them_is_a_custom_doctype_any_more(self):
        for d in _doctype_folders():
            spec = json.loads((d / f"{d.name}.json").read_text())
            with self.subTest(doctype=spec["name"]):
                self.assertFalse(spec.get("custom"),
                                 "custom: 1 keeps it out of migrate's path")
                self.assertEqual(spec.get("module"), "Zeloura")

    def test_the_site_agrees(self):
        for name in setup.EXPECTED_DOCTYPES:
            with self.subTest(doctype=name):
                self.assertTrue(frappe.db.exists("DocType", name))
                self.assertFalse(frappe.db.get_value("DocType", name, "custom"))


class TestNothingRebuildsADocTypeAtRuntime(FrappeTestCase):
    """The conversion in B1 is only worth anything if it stays converted.

    `setup_customer_accounts()` deleted `Customer Account` and `Customer Token`
    and rebuilt them from a Python dict with `custom: 1` — every call. That
    dropped every customer account and signed out every customer holding a
    token, and put both DocTypes back outside `bench migrate`'s path. The test
    suite called it to "ensure" they existed, which is how it was caught.
    """

    PAIR = ("Customer Account", "Customer Token")

    def test_the_endpoint_reports_and_does_not_rebuild(self):
        from luuvcrm import api
        before = {d: frappe.db.get_value("DocType", d, "modified") for d in self.PAIR}
        out = api.setup_customer_accounts()
        self.assertEqual(out["status"], "present")
        self.assertEqual(sorted(out["present"]), sorted(self.PAIR))
        for d in self.PAIR:
            with self.subTest(doctype=d):
                self.assertEqual(frappe.db.get_value("DocType", d, "modified"), before[d],
                                 "it touched the DocType")
                self.assertFalse(frappe.db.get_value("DocType", d, "custom"))

    def test_the_source_no_longer_deletes_them(self):
        source = frappe.read_file(frappe.get_app_path("luuvcrm", "api", "customers.py"))
        body = source[source.index("def setup_customer_accounts"):]
        body = body[:body.index("\ndef ")]
        self.assertNotIn("delete_doc", body)
        self.assertNotIn('"custom": 1', body)

    def test_the_profile_fields_ship_with_the_doctype(self):
        """They were added at runtime by a `dt.save()`, which is what marked the
        DocType custom on a site that did not have them yet."""
        spec = json.loads((pathlib.Path(frappe.get_app_path(
            "luuvcrm", "zeloura", "doctype", "customer_account"))
            / "customer_account.json").read_text())
        names = {f["fieldname"] for f in spec["fields"]}
        self.assertLessEqual({"address", "area"}, names)
        for column in ("address", "area"):
            with self.subTest(column=column):
                self.assertTrue(frappe.db.has_column("Customer Account", column))


class TestEveryControllerCanBeImported(FrappeTestCase):
    """The one that matters most in this file.

    `bench migrate` calls `remove_orphan_doctypes()`, which treats a DocType
    whose controller raises ImportError as orphaned **and deletes it**. The
    class name is not free: `import_controller` looks for
    `doctype.replace(" ", "").replace("-", "")` exactly — so `POS Approval`
    needs `POSApproval`, not `PosApproval`.

    Generating them with a plain title-case rule produced `PosApproval`,
    `QRPromoCode` as `QrPromoCode`, `LFActivityLog` as `LfActivityLog` and four
    more; the next migrate found seven orphans and deleted them. The tables and
    their rows survived — `remove_orphan_doctypes` is documented as
    non-destructive — but every field definition, permission and naming rule
    went, which on a live site is an app that no longer starts.
    """

    def test_the_class_name_is_the_one_frappe_looks_for(self):
        for d in _doctype_folders():
            spec = json.loads((d / f"{d.name}.json").read_text())
            src = (d / f"{d.name}.py").read_text()
            want = spec["name"].replace(" ", "").replace("-", "")
            with self.subTest(doctype=spec["name"]):
                self.assertIn(f"class {want}(", src,
                              f"migrate would call {spec['name']} orphaned and delete it")

    def test_frappe_can_actually_load_every_one(self):
        """Belt and braces: ask Frappe rather than pattern-matching the source.
        This is the exact call `remove_orphan_doctypes` makes."""
        from frappe.model.base_document import get_controller
        for name in setup.EXPECTED_DOCTYPES:
            with self.subTest(doctype=name):
                get_controller(name)

    def test_a_controller_file_exists_for_each(self):
        for d in _doctype_folders():
            with self.subTest(doctype=d.name):
                self.assertTrue((d / f"{d.name}.py").exists())
                self.assertTrue((d / "__init__.py").exists())


class TestTheShapeIsDeclaredInOnePlace(FrappeTestCase):
    def test_setup_doctype_no_longer_redeclares_the_fields(self):
        """It used to carry the full field list as Python dicts *and* the JSON
        shipped alongside — two definitions of the same thing, free to drift."""
        src = frappe.read_file(frappe.get_app_path("luuvcrm", "setup_doctype.py"))
        self.assertNotIn("DOCTYPES = [", src)
        self.assertNotIn("def _dt(", src)

    def test_it_checks_rather_than_creates(self):
        """`install_app` syncs the JSON before calling `after_install`, so by the
        time execute() runs they exist. Creating them again from Python is how
        the two definitions drift apart."""
        import inspect
        src = inspect.getsource(setup.ensure_doctypes)
        self.assertNotIn("insert(", src)


class TestThePackagingCategory(FrappeTestCase):
    """Packaging arrived after the P&L categories, so it has its own patch."""

    def setUp(self):
        if not frappe.db.exists("DocType", "Expense Category"):
            self.skipTest("no Expense Category on this site")
        if not frappe.db.count("Expense Category"):
            self.skipTest("no category to make the site look live")

    def _run(self):
        from luuvcrm.patches.v1_0 import add_packaging_expense_category
        add_packaging_expense_category.execute()

    def test_a_fresh_site_seeds_it(self):
        from luuvcrm.api.expenses import EXPENSE_CATEGORIES
        self.assertIn("Packaging", EXPENSE_CATEGORIES)

    def test_a_live_site_is_topped_up(self):
        frappe.db.delete("Expense Category", {"name": "Packaging"})
        self._run()
        self.assertTrue(frappe.db.exists("Expense Category", "Packaging"))

    def test_a_disabled_one_stays_disabled(self):
        self._run()
        frappe.db.set_value("Expense Category", "Packaging", "disabled", 1)
        self._run()
        self.assertEqual(frappe.db.get_value("Expense Category", "Packaging", "disabled"), 1)


class TestThePnlExpenseCategories(FrappeTestCase):
    """The profit-and-loss statement has a line for each of these. A live site's
    Expense Category table is not empty, so the seed never runs there — the patch
    is the only way they arrive."""

    def setUp(self):
        from luuvcrm.api.expenses import PNL_EXPENSE_CATEGORIES
        if not frappe.db.exists("DocType", "Expense Category"):
            self.skipTest("no Expense Category on this site")
        self.wanted = PNL_EXPENSE_CATEGORIES

    def _drop(self):
        for name in self.wanted:
            frappe.db.delete("Expense Category", {"name": name})

    def _run(self):
        from luuvcrm.patches.v1_0 import add_pnl_expense_categories
        add_pnl_expense_categories.execute()

    def test_there_are_eight_and_a_fresh_site_seeds_them_too(self):
        from luuvcrm.api.expenses import EXPENSE_CATEGORIES
        self.assertEqual(len(self.wanted), 8)
        self.assertTrue(set(self.wanted) <= set(EXPENSE_CATEGORIES))
        self.assertEqual(len(EXPENSE_CATEGORIES), len(set(EXPENSE_CATEGORIES)))

    def test_a_live_site_is_topped_up(self):
        if not frappe.db.count("Expense Category"):
            self.skipTest("no category to make the site look live")
        self._drop()
        frappe.get_doc({"doctype": "Expense Category", "category_name": "_LFT Rent"}).insert(
            ignore_permissions=True)
        self._run()
        for name in self.wanted:
            self.assertTrue(frappe.db.exists("Expense Category", name), name)

    def test_running_it_twice_changes_nothing(self):
        self._run()
        before = frappe.db.count("Expense Category")
        self._run()
        self.assertEqual(frappe.db.count("Expense Category"), before)

    def test_a_category_the_manager_disabled_is_left_disabled(self):
        self._run()
        frappe.db.set_value("Expense Category", self.wanted[0], "disabled", 1)
        self._run()
        self.assertEqual(frappe.db.get_value("Expense Category", self.wanted[0], "disabled"), 1)

    def test_an_empty_table_is_not_refilled(self):
        """Empty means a manager cleared it on purpose, or a fresh site whose
        seed has it covered."""
        frappe.db.delete("Expense Category")
        self._run()
        self.assertEqual(frappe.db.count("Expense Category"), 0)
