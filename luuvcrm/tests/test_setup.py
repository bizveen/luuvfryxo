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


class TestTheHookIsWired(FrappeTestCase):
    def test_after_install_runs_it(self):
        from luuvcrm import hooks
        self.assertEqual(hooks.after_install, "luuvcrm.setup_doctype.after_install")

    def test_it_is_not_wired_to_after_migrate(self):
        """Every step is create-only, but a live deploy is the wrong place to
        find out we were wrong about that."""
        from luuvcrm import hooks
        self.assertFalse(getattr(hooks, "after_migrate", None))
