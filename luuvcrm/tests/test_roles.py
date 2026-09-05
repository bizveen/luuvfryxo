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
        self.assertEqual(roles.ensure_roles(), {"created": [], "enabled": []},
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
