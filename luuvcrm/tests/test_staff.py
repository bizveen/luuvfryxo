"""Hiring, roles, branches, and the way in.

Adding a waiter used to mean the Frappe Desk: create a User, find the right
role in a list of ninety, and know that a User Permission on Restaurant is what
keeps a branch manager to one branch. These endpoints are that job, done by the
owner, and what they refuse matters more than what they do.

Two refusals carry the weight. **System Manager cannot be granted here** — it
bypasses every guard in `roles.py`, so a screen that could hand it out would
make every other rule in this app advisory. And **no password is ever typed**:
the owner sends a one-time link, the staffer chooses their own.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles


class StaffTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.user("lf-test-staff-owner@example.com", roles.RESTAURANT_OWNER)
        self.as_user(self.owner)
        self._made = []
        self.addCleanup(self._drop)

    def _drop(self):
        previous = frappe.session.user
        frappe.set_user("Administrator")
        try:
            for email in self._made:
                if frappe.db.exists("User", email):
                    frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        finally:
            frappe.set_user(previous)

    def user(self, email, role):
        """Fixture users are made as Administrator on purpose: creating a User is
        a Desk-permission act, and the whole point of the endpoints below is that
        the owner does not hold that permission."""
        previous = frappe.session.user
        frappe.set_user("Administrator")
        try:
            if not frappe.db.exists("User", email):
                frappe.get_doc({"doctype": "User", "email": email, "first_name": "Staff Test",
                                "send_welcome_email": 0, "roles": [{"role": role}]}
                               ).insert(ignore_permissions=True)
            doc = frappe.get_doc("User", email)
            doc.flags.ignore_permissions = True
            doc.add_roles(role)
        finally:
            frappe.set_user(previous)
        return email

    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def hire(self, email=None, role=None, **kwargs):
        """A fresh email per test. Inserting a User commits in Frappe, so the
        suite's rollback does not take one back out — a fixture that reuses one
        address fails the *next* test with "already has an account"."""
        email = email or self.new_email()
        self._made.append(email)
        return api.create_staff(email=email, full_name="New Waiter",
                                role=role or roles.WAITER, **kwargs)

    def new_email(self):
        return "lf-test-hire-%s@example.com" % frappe.generate_hash(length=8)


class TestHiring(StaffTestCase):
    def test_a_new_person_gets_an_account_and_a_role(self):
        result = self.hire()
        self.assertTrue(result["ok"])
        self.assertIn(roles.WAITER, result["staff"]["roles"])
        self.assertEqual(result["staff"]["enabled"], 1)

    def test_no_password_is_set_or_asked_for(self):
        """The account exists and cannot be signed into until the person uses
        the invite link. That is the point: choosing somebody else's password is
        not the owner's to do."""
        import inspect

        from luuvcrm.api import staff

        source = inspect.getsource(staff)
        for word in ("new_password", "pwd=", "update_password"):
            self.assertNotIn(word, source, "this screen must never handle a password")

    def test_the_invite_link_is_a_one_time_link_and_not_a_password(self):
        who = self.hire()["user"]
        link = api.staff_invite_link(user=who)["link"]
        self.assertIn("/update-password?key=", link)

    def test_an_invite_for_a_switched_off_account_is_refused(self):
        who = self.hire()["user"]
        api.set_staff_enabled(user=who, enabled=0)
        with self.assertRaises(frappe.ValidationError):
            api.staff_invite_link(user=who)

    def test_a_second_account_for_the_same_person_is_refused(self):
        who = self.hire()["user"]
        with self.assertRaises(frappe.ValidationError):
            self.hire(email=who)

    def test_a_nonsense_email_is_refused(self):
        with self.assertRaises(Exception):
            api.create_staff(email="not-an-email", full_name="X", role=roles.WAITER)

    def test_they_can_be_put_on_a_branch_as_they_are_hired(self):
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        if not restaurant:
            self.skipTest("no Restaurant on this site")
        who = self.hire(role=roles.RESTAURANT_MANAGER, restaurant=restaurant)["user"]
        self.assertIn(restaurant, branch.assigned(who))


class TestWhatItRefusesToGrant(StaffTestCase):
    def test_system_manager_cannot_be_granted(self):
        """It bypasses every guard in roles.py. A screen that could hand it out
        would make every other rule in this app advisory."""
        who = self.hire()["user"]
        with self.assertRaises(frappe.ValidationError):
            api.set_staff_role(user=who, role=roles.SYSTEM_MANAGER, on=1)

    def test_a_role_this_app_does_not_own_cannot_be_granted(self):
        who = self.hire()["user"]
        with self.assertRaises(frappe.ValidationError):
            api.set_staff_role(user=who, role="Accounts Manager", on=1)

    def test_nobody_can_be_hired_straight_into_system_manager(self):
        with self.assertRaises(frappe.ValidationError):
            api.create_staff(email="lf-test-admin@example.com", full_name="Admin",
                             role=roles.SYSTEM_MANAGER)


class TestRolesAndBranches(StaffTestCase):
    def test_a_role_can_be_given_and_taken(self):
        who = self.hire()["user"]
        api.set_staff_role(user=who, role=roles.CASHIER, on=1)
        self.assertIn(roles.CASHIER, api.set_staff_role(user=who, role=roles.WAITER,
                                                        on=0)["staff"]["roles"])
        self.assertNotIn(roles.WAITER, frappe.get_roles(who))

    def test_an_owner_cannot_take_the_owner_role_off_themselves(self):
        """The only door out of the building. Somebody has to keep the key."""
        with self.assertRaises(frappe.ValidationError):
            api.set_staff_role(user=self.owner, role=roles.RESTAURANT_OWNER, on=0)

    def test_an_owner_cannot_switch_off_their_own_account(self):
        with self.assertRaises(frappe.ValidationError):
            api.set_staff_enabled(user=self.owner, enabled=0)

    def test_restricting_an_owner_to_a_branch_is_refused(self):
        """The absence of a User Permission is what makes somebody an owner, so
        adding one would quietly demote them."""
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        if not restaurant:
            self.skipTest("no Restaurant on this site")
        with self.assertRaises(frappe.ValidationError):
            api.set_staff_branch(user=self.owner, restaurant=restaurant)

    def test_a_manager_can_be_moved_between_branches(self):
        restaurant = frappe.db.get_value("Restaurant", {}, "name")
        if not restaurant:
            self.skipTest("no Restaurant on this site")
        who = self.hire(role=roles.RESTAURANT_MANAGER)["user"]
        api.set_staff_branch(user=who, restaurant=restaurant)
        self.assertIn(restaurant, branch.assigned(who))
        api.set_staff_branch(user=who, restaurant=None)
        self.assertEqual(branch.assigned(who), [])

    def test_switching_someone_off_never_deletes_them(self):
        """Every order, shift and trail row names its user. Deleting the User
        takes the name off all of it."""
        who = self.hire()["user"]
        api.set_staff_enabled(user=who, enabled=0)
        self.assertTrue(frappe.db.exists("User", who))
        self.assertEqual(frappe.db.get_value("User", who, "enabled"), 0)


class TestWhoMayManageStaff(StaffTestCase):
    def test_a_manager_may_read_but_not_write(self):
        """A manager covering a shift needs to know who is on the floor. Hiring
        is the owner's."""
        manager = self.user("lf-test-staff-manager@example.com", roles.RESTAURANT_MANAGER)
        self.as_user(manager)
        listing = api.get_staff()
        self.assertIn("staff", listing)
        self.assertFalse(listing["can_edit"])
        with self.assertRaises(frappe.PermissionError):
            api.create_staff(email="x@example.com", full_name="X", role=roles.WAITER)

    def test_a_cashier_may_not_even_read(self):
        cashier = self.user("lf-test-staff-cashier@example.com", roles.CASHIER)
        self.as_user(cashier)
        with self.assertRaises(frappe.PermissionError):
            api.get_staff()

    def test_the_listing_names_this_app_s_roles_only(self):
        listing = api.get_staff()
        self.assertEqual(set(listing["roles"]), set(roles.LUUVCRM_ROLES))
        self.assertNotIn(roles.SYSTEM_MANAGER, listing["roles"])

    def test_administrator_is_not_listed_as_staff(self):
        self.assertNotIn("Administrator", [p["user"] for p in api.get_staff()["staff"]])
