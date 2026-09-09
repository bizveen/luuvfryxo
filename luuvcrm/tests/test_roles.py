"""The role model: who may do what.

Unlike test_security.py, which asserts the *shape* of the API surface, these
tests create real users, give them real roles, and ask Frappe the same question
a request would. That is the only way to catch a DocPerm that was never
migrated, or a guard that passes for the wrong reason.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import roles


def _user(role):
    """A throwaway user holding exactly one LuuvCRM role (plus the base roles
    Frappe gives everybody)."""
    email = f"lf-test-{role.lower().replace(' ', '-')}@example.com"
    if frappe.db.exists("User", email):
        frappe.delete_doc("User", email, force=True, ignore_permissions=True)
    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": f"Test {role}",
        "send_welcome_email": 0,
        "roles": [{"role": role}],
    })
    user.insert(ignore_permissions=True)
    return email


class TestRolesExist(FrappeTestCase):
    def test_every_declared_role_is_installed(self):
        """luuvcrm.patches.v1_0.create_roles ships them; a site that skipped the
        patch would fail here rather than at the first click."""
        missing = [r for r in roles.LUUVCRM_ROLES if not frappe.db.exists("Role", r)]
        self.assertEqual(missing, [], f"roles not created by the patch: {missing}")

    def test_ensure_roles_is_idempotent(self):
        self.assertEqual(roles.ensure_roles(),
                         {"created": [], "enabled": [], "narrowed": []},
                         "ensure_roles must change nothing on a site that is already correct")

    def test_no_required_role_is_disabled(self):
        """frappe.get_roles() omits disabled roles, so a disabled one means every
        holder silently has no permissions — which is how Restaurant Manager
        arrived on the test site."""
        disabled = [r for r in roles.LUUVCRM_ROLES
                    if frappe.db.get_value("Role", r, "disabled")]
        self.assertEqual(disabled, [], f"roles the app requires are disabled: {disabled}")

    def test_ensure_roles_re_enables_a_disabled_role(self):
        role = roles.RESTAURANT_MANAGER
        frappe.db.set_value("Role", role, "disabled", 1)
        self.addCleanup(frappe.db.set_value, "Role", role, "disabled", 0)
        self.assertEqual(roles.ensure_roles()["enabled"], [role])
        self.assertFalse(frappe.db.get_value("Role", role, "disabled"))

    def test_floor_roles_have_no_desk_access(self):
        """A waiter and the kitchen work entirely in the web apps. Desk access on
        a shared floor phone is the whole ERPNext back office one tap away."""
        wide = [r for r in roles.LUUVCRM_ROLES
                if r not in roles.DESK_ROLES
                and frappe.db.get_value("Role", r, "desk_access")]
        self.assertEqual(wide, [], f"floor roles that can open the Desk: {wide}")

    def test_ensure_roles_takes_desk_access_off_a_floor_role(self):
        role = roles.WAITER
        frappe.db.set_value("Role", role, "desk_access", 1)
        self.addCleanup(frappe.db.set_value, "Role", role, "desk_access", 0)
        self.assertEqual(roles.ensure_roles()["narrowed"], [role])
        self.assertFalse(frappe.db.get_value("Role", role, "desk_access"))

    def test_ensure_roles_never_hands_desk_access_back(self):
        """Widening is somebody's decision, not an installer's: a cashier whose
        desk access was deliberately removed keeps it removed."""
        role = roles.CASHIER
        self.assertIn(role, roles.DESK_ROLES)
        frappe.db.set_value("Role", role, "desk_access", 0)
        self.addCleanup(frappe.db.set_value, "Role", role, "desk_access", 1)
        roles.ensure_roles()
        self.assertFalse(frappe.db.get_value("Role", role, "desk_access"),
                         "ensure_roles must not widen an existing role")

    def test_drift_names_both_directions_without_fixing_either(self):
        frappe.db.set_value("Role", roles.KITCHEN_STAFF, "desk_access", 1)
        frappe.db.set_value("Role", roles.RESTAURANT_MANAGER, "desk_access", 0)
        self.addCleanup(frappe.db.set_value, "Role", roles.KITCHEN_STAFF, "desk_access", 0)
        self.addCleanup(frappe.db.set_value, "Role", roles.RESTAURANT_MANAGER, "desk_access", 1)
        drift = roles.desk_access_drift()
        self.assertIn(roles.KITCHEN_STAFF, drift["too_wide"])
        self.assertIn(roles.RESTAURANT_MANAGER, drift["too_narrow"])
        self.assertTrue(frappe.db.get_value("Role", roles.KITCHEN_STAFF, "desk_access"),
                        "the report must not change anything")

    def test_owner_is_a_manager_but_a_manager_is_not_an_owner(self):
        self.assertIn(roles.RESTAURANT_OWNER, roles.MANAGER_ROLES)
        self.assertNotIn(roles.RESTAURANT_MANAGER, roles.OWNER_ROLES)

    def test_system_manager_is_not_treated_as_a_business_role(self):
        """It is in the sets because it must be able to fix things, but it is
        Frappe's admin role and is never one of this app's own."""
        self.assertNotIn(roles.SYSTEM_MANAGER, roles.LUUVCRM_ROLES)
        self.assertNotIn(roles.ADMINISTRATOR, roles.LUUVCRM_ROLES)


class TestRoleHierarchy(FrappeTestCase):
    """Each level contains the one below it, so a cashier can cover the floor
    and an owner can cover a till."""

    def test_each_level_contains_the_one_below(self):
        self.assertLessEqual(roles.ADMIN_ROLES, roles.OWNER_ROLES)
        self.assertLessEqual(roles.OWNER_ROLES, roles.MANAGER_ROLES)
        self.assertLessEqual(roles.MANAGER_ROLES, roles.POS_ROLES)
        self.assertLessEqual(roles.POS_ROLES, roles.WAITER_ROLES)
        self.assertLessEqual(roles.WAITER_ROLES, roles.KITCHEN_ROLES)

    def test_kitchen_staff_reach_only_the_kitchen(self):
        for allowed in (roles.MANAGER_ROLES, roles.POS_ROLES, roles.WAITER_ROLES):
            self.assertNotIn(roles.KITCHEN_STAFF, allowed)
        self.assertIn(roles.KITCHEN_STAFF, roles.KITCHEN_ROLES)

    def test_a_waiter_cannot_take_payment_or_read_reports(self):
        self.assertNotIn(roles.WAITER, roles.POS_ROLES)
        self.assertNotIn(roles.WAITER, roles.MANAGER_ROLES)

    def test_a_cashier_cannot_read_reports(self):
        self.assertNotIn(roles.CASHIER, roles.MANAGER_ROLES)


class TestGuards(FrappeTestCase):
    """The guards behind every endpoint, asked as a real user."""

    def _as(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def test_a_waiter_is_refused_the_manager_reports(self):
        self._as(_user(roles.WAITER))
        with self.assertRaises(frappe.PermissionError):
            roles.require_manager()

    def test_a_cashier_is_refused_the_manager_reports(self):
        self._as(_user(roles.CASHIER))
        with self.assertRaises(frappe.PermissionError):
            roles.require_manager()

    def test_a_cashier_may_work_the_till(self):
        self._as(_user(roles.CASHIER))
        roles.require_pos()  # must not raise

    def test_a_manager_may_do_everything_a_cashier_may(self):
        self._as(_user(roles.RESTAURANT_MANAGER))
        roles.require_manager()
        roles.require_pos()
        roles.require_waiter()

    def test_an_owner_passes_the_owner_guard_and_a_manager_does_not(self):
        self._as(_user(roles.RESTAURANT_OWNER))
        roles.require_owner()
        frappe.set_user(_user(roles.RESTAURANT_MANAGER))
        with self.assertRaises(frappe.PermissionError):
            roles.require_owner()

    def test_only_an_admin_passes_require_admin(self):
        self._as(_user(roles.RESTAURANT_OWNER))
        with self.assertRaises(frappe.PermissionError):
            roles.require_admin()

    def test_kitchen_staff_are_refused_the_till(self):
        self._as(_user(roles.KITCHEN_STAFF))
        with self.assertRaises(frappe.PermissionError):
            roles.require_pos()
        roles.require_kitchen()  # must not raise


class TestPosOrderPermissions(FrappeTestCase):
    """The Cashier gap: POS Order granted write to every role except the one
    that actually works the till, which is why update_order carried
    ignore_permissions."""

    def test_a_cashier_may_write_a_pos_order(self):
        email = _user(roles.CASHIER)
        self.assertTrue(frappe.has_permission("POS Order", "write", user=email),
                        "a cashier must be able to edit an open bill")
        self.assertTrue(frappe.has_permission("POS Order", "create", user=email))

    def test_update_order_no_longer_bypasses_permissions(self):
        import inspect

        from luuvcrm import api
        body = inspect.getsource(api.update_order)
        head = body.split("if pos_order.pos_invoice:")[0]
        self.assertNotIn("pos_order.flags.ignore_permissions", head,
                         "the order save must run under the caller's permissions")

    def test_every_role_that_uses_an_order_can_read_one(self):
        for role in (roles.CASHIER, roles.WAITER, roles.KITCHEN_STAFF,
                     roles.RESTAURANT_MANAGER, roles.RESTAURANT_OWNER):
            with self.subTest(role=role):
                self.assertTrue(frappe.has_permission("POS Order", "read", user=_user(role)))

    def test_only_managers_may_delete_an_order(self):
        for role, may in ((roles.CASHIER, False), (roles.WAITER, False),
                          (roles.KITCHEN_STAFF, False),
                          (roles.RESTAURANT_MANAGER, True), (roles.RESTAURANT_OWNER, True)):
            with self.subTest(role=role):
                self.assertEqual(
                    bool(frappe.has_permission("POS Order", "delete", user=_user(role))), may)

    def test_kitchen_staff_cannot_create_an_order(self):
        self.assertFalse(
            frappe.has_permission("POS Order", "create", user=_user(roles.KITCHEN_STAFF)),
            "the kitchen advances orders, it does not open them")


class TestNoDuplicateRoleSets(FrappeTestCase):
    """Four copies of the role sets lived in three files and drifted apart. One
    module owns them now."""

    def test_no_module_declares_its_own_role_set(self):
        """The check that keeps them from drifting apart again.

        `luuvcrm.api` is a package now, so this walks every module in it rather
        than reading one file — which is the point: 22 modules is 22 chances to
        paste a role set back in."""
        import inspect
        import pkgutil
        import importlib

        from luuvcrm import activity, api
        from luuvcrm.www import manager

        modules = [activity, manager, api]
        modules += [importlib.import_module(f"luuvcrm.api.{m.name}")
                    for m in pkgutil.iter_modules(api.__path__)]
        for module in modules:
            with self.subTest(module=module.__name__):
                self.assertNotIn('ROLES = {"', inspect.getsource(module),
                                 f"{module.__name__} declares its own role set again")

    def test_the_modules_that_gate_on_roles_get_them_from_one_place(self):
        import importlib

        from luuvcrm import activity, roles
        from luuvcrm.www import manager

        gated = [activity, manager,
                 importlib.import_module("luuvcrm.api.approvals"),
                 importlib.import_module("luuvcrm.api.purchases"),
                 importlib.import_module("luuvcrm.api.reports")]
        for module in gated:
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(module, "roles", None), roles,
                              f"{module.__name__} should gate through luuvcrm.roles")


class TestTheDocPermsTheScreensNeed(FrappeTestCase):
    """Until Phase 2c nothing read through `frappe.get_list`, so the permission
    table was decoration and nobody noticed it was wrong.

    It was wrong in the direction that matters: a Restaurant Manager could not
    read Purchase Bill, Expense, Wastage or POS Approval — their own screens —
    while a Cashier could, and Restaurant Table was manager-only, so the till
    would have lost its table map the moment a read started applying
    permissions. These tests are what stop that coming back, because the symptom
    is a blank screen rather than an error.
    """

    def test_granting_survives_rows_frappe_itself_would_refuse(self):
        """The live site's Restaurant has *standard* DocPerm rows with
        `import: 1` on a DocType that is not importable, and System Manager
        twice at level 0. Frappe validates the standard rows on every
        permission write and refuses — which is how the deploy rehearsal died
        in `grant_app_permissions`. Rebuild that state and grant through it."""
        doctype = "Restaurant"
        if not frappe.db.exists("DocType", doctype):
            self.skipTest("hospitality is not installed")
        allow_import_before = frappe.db.get_value("DocType", doctype, "allow_import")
        std_before = frappe.get_all("DocPerm", filters={"parent": doctype}, fields=["*"])
        custom_before = frappe.get_all("Custom DocPerm", filters={"parent": doctype}, fields=["*"])
        self.assertTrue(std_before, "hospitality ships Restaurant with permissions")

        def _restore(table, rows):
            frappe.db.delete(table, {"parent": doctype})
            for row in rows:
                frappe.get_doc({**row, "doctype": table}).db_insert()

        try:
            frappe.db.set_value("DocType", doctype, "allow_import", 0, update_modified=False)
            frappe.db.delete("Custom DocPerm", {"parent": doctype})
            first = std_before[0]
            frappe.db.set_value("DocPerm", first.name, "import", 1, update_modified=False)
            twin = {**first, "name": None, "idx": len(std_before) + 1, "import": 0, "write": 1}
            frappe.get_doc({**twin, "doctype": "DocPerm"}).db_insert()
            frappe.clear_cache(doctype=doctype)

            fixed = roles._perms_valid(doctype)
            self.assertTrue(any("merged" in f for f in fixed), fixed)
            self.assertTrue(any("import off" in f for f in fixed), fixed)
            for table in ("DocPerm", "Custom DocPerm"):
                rows = frappe.get_all(table, filters={"parent": doctype, "role": first.role,
                                                      "permlevel": first.permlevel},
                                      fields=["read", "write", "import"])
                self.assertEqual(len(rows), 1, f"{table}: one row per role and level")
                self.assertEqual((rows[0].write, rows[0]["import"]), (1, 0),
                                 f"{table}: the merged row carries the OR of the flags, import off")

            roles.ensure_app_docperms()          # the call that used to raise
            self.assertTrue(_role_may(roles.CASHIER, doctype, "read"))
        finally:
            _restore("DocPerm", std_before)
            _restore("Custom DocPerm", custom_before)
            frappe.db.set_value("DocType", doctype, "allow_import", allow_import_before,
                                update_modified=False)
            frappe.clear_cache(doctype=doctype)

    def test_the_site_has_everything_the_table_asks_for(self):
        self.assertEqual(roles.missing_docperms(), {},
                         "run luuvcrm.patches.v1_0.grant_app_permissions")

    def test_granting_again_changes_nothing(self):
        before = roles.missing_docperms()
        roles.ensure_app_docperms()
        self.assertEqual(roles.missing_docperms(), before)

    def test_a_manager_can_read_their_own_screens(self):
        for doctype in ("Purchase Bill", "Expense", "Wastage", "POS Approval",
                        "POS Cash Movement", "LF Activity Log"):
            with self.subTest(doctype=doctype):
                self.assertTrue(
                    _role_may(roles.RESTAURANT_MANAGER, doctype, "read"),
                    f"a manager cannot read {doctype}")

    def test_an_owner_can_read_everything_a_manager_can(self):
        """DocPerms do not inherit — OWNER_ROLES being a superset governs this
        app's own guards and nothing Frappe does."""
        for doctype, by_perm in roles.APP_DOCPERMS.items():
            if not frappe.db.exists("DocType", doctype):
                continue
            if roles.RESTAURANT_MANAGER not in by_perm.get("read", ()):
                continue
            with self.subTest(doctype=doctype):
                self.assertTrue(_role_may(roles.RESTAURANT_OWNER, doctype, "read"))

    def test_the_till_can_still_see_the_tables(self):
        for role in (roles.CASHIER, roles.WAITER, roles.KITCHEN_STAFF):
            with self.subTest(role=role):
                self.assertTrue(_role_may(role, "Restaurant Table", "read"))

    def test_the_till_may_raise_a_request_but_not_answer_it(self):
        """Separation of duties, in the permission table as well as in the
        guard: a cashier creates an approval, a manager decides it."""
        self.assertTrue(_role_may(roles.CASHIER, "POS Approval", "create"))
        self.assertFalse(_role_may(roles.CASHIER, "POS Approval", "write"))
        self.assertTrue(_role_may(roles.RESTAURANT_MANAGER, "POS Approval", "write"))

    def test_no_guard_can_be_walked_around_through_the_generic_api(self):
        """Frappe exposes every doctype over its own REST API, where DocPerms
        are the only thing in the way — so a rule enforced in an endpoint alone
        is advisory. This is the list of grants that made one so."""
        self.assertEqual(roles.walkable_docperms(), {},
                         "run luuvcrm.patches.v1_0.grant_app_permissions")

    def test_the_till_cannot_edit_what_the_restaurant_owes_a_guide(self):
        """`cashback_balance` is money, and the txn rows are its ledger."""
        for perm in ("write", "create", "delete"):
            with self.subTest(perm=perm):
                self.assertFalse(_role_may(roles.CASHIER, "Tour Guide", perm))
                self.assertFalse(_role_may(roles.CASHIER, "Guide Cashback Txn", perm))
        self.assertTrue(_role_may(roles.CASHIER, "Tour Guide", "read"))

    def test_nobody_at_the_till_can_erase_a_trail(self):
        for doctype in ("POS Approval", "Wastage", "POS Cash Movement"):
            with self.subTest(doctype=doctype):
                self.assertFalse(_role_may(roles.CASHIER, doctype, "delete"))

    def test_revoking_again_changes_nothing(self):
        self.assertEqual(roles.revoke_app_docperms(), {})

    def test_the_staff_trail_is_manager_reading_only(self):
        self.assertFalse(_role_may(roles.CASHIER, "LF Activity Log", "read"))
        self.assertFalse(_role_may(roles.WAITER, "LF Activity Log", "read"))
        self.assertTrue(_role_may(roles.RESTAURANT_MANAGER, "LF Activity Log", "read"))


def _role_may(role, doctype, perm):
    rows = frappe.get_all("Custom DocPerm", filters={"parent": doctype, "role": role},
                          fields=[perm])
    if not rows:
        rows = frappe.get_all("DocPerm", filters={"parent": doctype, "role": role},
                              fields=[perm])
    return any(r.get(perm) for r in rows)
