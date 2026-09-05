"""The Owner's screen.

Phase 2c built the multi-restaurant machinery — a `restaurant` link on every
row, a User Permission per manager, `get_list` applying it — and left it
reachable only from a Python console. `roles.require_owner()` existed and
nothing called it. `/owner` is what gives the role something to do.

Two jobs, and the tests follow them: **compare** (every other report sums the
branches, which answers the wrong question when you own two) and **set up** (a
Cost Center each, a Warehouse each, a till that names its branch, a manager
restricted to theirs — four things somebody does once in the Desk, forgets, and
cannot tell from working).
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles
from luuvcrm.tests import fixtures as fx


def _user(tag, role):
    email = f"lf-test-own-{tag}@example.com"
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": "Owner Test",
                        "send_welcome_email": 0, "roles": [{"role": role}]
                        }).insert(ignore_permissions=True)
    return email


def _drop_test_restaurants():
    for name in frappe.get_all("Restaurant", filters={"name": ["like", fx.PREFIX + "%"]},
                               pluck="name"):
        for dt in branch.BRANCHED_DOCTYPES:
            if frappe.db.exists("DocType", dt) and frappe.db.has_column(dt, branch.FIELD):
                frappe.db.sql(f"update `tab{dt}` set `{branch.FIELD}` = null "
                              f"where `{branch.FIELD}` = %s", (name,))
        if frappe.db.has_column("POS Profile", branch.FIELD):
            frappe.db.sql("update `tabPOS Profile` set `restaurant` = null "
                          "where `restaurant` = %s", (name,))
        frappe.delete_doc("Restaurant", name, force=True, ignore_permissions=True)
    frappe.db.commit()


class OwnerTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        _drop_test_restaurants()
        self.addCleanup(_drop_test_restaurants)
        self.owner = _user("owner", roles.RESTAURANT_OWNER)
        self.manager = _user("mgr", roles.RESTAURANT_MANAGER)
        branch.unassign(self.owner)
        self.addCleanup(branch.unassign, self.manager)
        self.home = frappe.get_all("Restaurant", pluck="name")[0]

    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def second_branch(self, tag="Colombo"):
        self.as_user(self.owner)
        name = api.create_branch(restaurant_name=fx.PREFIX + tag)["restaurant"]
        frappe.set_user("Administrator")
        return name

    def cost_centre(self, tag):
        company = frappe.db.get_value("Company", {}, "name")
        name = fx.PREFIX + tag
        existing = frappe.db.get_value("Cost Center", {"cost_center_name": name}, "name")
        if existing:
            return existing
        parent = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
        doc = frappe.get_doc({"doctype": "Cost Center", "cost_center_name": name,
                              "company": company, "parent_cost_center": parent,
                              "is_group": 0}).insert(ignore_permissions=True)
        self.addCleanup(lambda: frappe.db.exists("Cost Center", doc.name) and
                        frappe.delete_doc("Cost Center", doc.name, force=True,
                                          ignore_permissions=True))
        return doc.name


class TestOnlyTheOwnerGetsIn(OwnerTestCase):
    """A manager can read their own branch's numbers on /manager. This screen is
    about comparing branches and moving a till or a person between them, and
    that is the owner's call."""

    def test_a_manager_is_refused_every_endpoint(self):
        self.as_user(self.manager)
        for call in (lambda: api.get_owner_overview(),
                     lambda: api.get_branch_setup(),
                     lambda: api.create_branch(restaurant_name="_LFT-nope"),
                     lambda: api.update_branch(restaurant=self.home),
                     lambda: api.assign_till(pos_profile="x", restaurant=self.home),
                     lambda: api.assign_manager(user=self.manager, restaurant=self.home)):
            with self.subTest(call=call):
                with self.assertRaises(frappe.PermissionError):
                    call()

    def test_the_page_turns_a_manager_away(self):
        import importlib
        page = importlib.import_module("luuvcrm.www.owner")
        self.as_user(self.manager)
        with self.assertRaises(frappe.PermissionError):
            page.get_context(frappe._dict())

    def test_a_guest_is_sent_to_the_login(self):
        import importlib
        page = importlib.import_module("luuvcrm.www.owner")
        self.as_user("Guest")
        with self.assertRaises(frappe.Redirect):
            page.get_context(frappe._dict())
        self.assertEqual(frappe.local.flags.redirect_location,
                         "/pos-login?redirect=/owner")


class TestTheComparison(OwnerTestCase):
    def test_one_branch_per_column(self):
        other = self.second_branch()
        self.as_user(self.owner)
        names = [b["restaurant"] for b in api.get_owner_overview()["branches"]]
        self.assertIn(self.home, names)
        self.assertIn(other, names)

    def test_a_branch_with_no_cost_centre_reports_nothing_not_zero(self):
        """"Rs 0 today" and "this branch's takings cannot be told from the other's"
        are different sentences, and only one of them is true."""
        other = self.second_branch()
        self.as_user(self.owner)
        row = next(b for b in api.get_owner_overview()["branches"]
                   if b["restaurant"] == other)
        self.assertIsNone(row["revenue"])
        self.assertIsNone(row["cash"])

    def test_once_it_has_one_the_numbers_appear(self):
        other = self.second_branch()
        centre = self.cost_centre("OWN-CC")
        self.as_user(self.owner)
        api.update_branch(restaurant=other, cost_center=centre)
        row = next(b for b in api.get_owner_overview()["branches"]
                   if b["restaurant"] == other)
        self.assertEqual(row["revenue"], 0.0)
        self.assertEqual(row["cost_center"], centre)

    def test_approvals_are_counted_per_branch(self):
        """The one number on this screen that is a call to action."""
        other = self.second_branch()
        doc = frappe.get_doc({"doctype": "POS Approval", "approval_type": "discount",
                              "status": "pending", "requested_by": self.manager,
                              branch.FIELD: other})
        doc.insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "POS Approval", doc.name, force=True,
                        ignore_permissions=True)
        self.as_user(self.owner)
        row = next(b for b in api.get_owner_overview()["branches"]
                   if b["restaurant"] == other)
        self.assertEqual(row["approvals_pending"], 1)

    def test_it_says_what_is_not_set_up(self):
        self.second_branch()
        self.as_user(self.owner)
        result = api.get_owner_overview()
        self.assertFalse(result["single_branch"])
        self.assertTrue(result["problems"], "a branch with no cost centre is a problem")

    def test_one_restaurant_is_not_a_problem(self):
        """Nothing to keep apart yet — the screen must not nag a single-branch
        site about configuration it does not need."""
        self.as_user(self.owner)
        result = api.get_owner_overview()
        self.assertTrue(result["single_branch"])
        self.assertEqual(result["problems"], [])


class TestSettingABranchUp(OwnerTestCase):
    def test_adding_one_copies_what_a_second_outlet_shares(self):
        """Company, default customer, menu and service charge belong to the
        business, not the outlet — asking for them again is a way to get them
        wrong."""
        other = self.second_branch()
        row = frappe.db.get_value("Restaurant", other,
                                  ["company", "default_customer", "active_menu"], as_dict=True)
        home = frappe.db.get_value("Restaurant", self.home,
                                   ["company", "default_customer", "active_menu"], as_dict=True)
        self.assertEqual(row.company, home.company)
        self.assertEqual(row.default_customer, home.default_customer)

    def test_a_duplicate_name_is_refused(self):
        self.as_user(self.owner)
        with self.assertRaises(frappe.ValidationError):
            api.create_branch(restaurant_name=self.home)

    def test_a_group_cost_centre_is_refused(self):
        """The Link field accepts it and the ledger then refuses to post to it."""
        other = self.second_branch()
        group = frappe.db.get_value("Cost Center", {"is_group": 1}, "name")
        self.as_user(self.owner)
        with self.assertRaises(frappe.ValidationError):
            api.update_branch(restaurant=other, cost_center=group)

    def test_a_till_can_be_pointed_at_a_branch(self):
        """The hop branch.current() relies on: with one Company shared across
        branches, POS Profile.company identifies nothing."""
        other = self.second_branch()
        till = frappe.db.get_value("POS Profile", {}, "name")
        self.as_user(self.owner)
        api.assign_till(pos_profile=till, restaurant=other)
        self.addCleanup(frappe.db.set_value, "POS Profile", till, branch.FIELD, None)
        self.assertEqual(frappe.db.get_value("POS Profile", till, branch.FIELD), other)
        self.assertEqual(branch._restaurant_for_profile(till), other)

    def test_a_manager_can_be_restricted_to_one(self):
        other = self.second_branch()
        self.as_user(self.owner)
        api.assign_manager(user=self.manager, restaurant=other)
        self.assertEqual(branch.assigned(self.manager), [other])

    def test_and_the_restriction_can_be_lifted(self):
        other = self.second_branch()
        self.as_user(self.owner)
        api.assign_manager(user=self.manager, restaurant=other)
        api.assign_manager(user=self.manager)
        self.assertEqual(branch.assigned(self.manager), [])

    def test_moving_a_manager_does_not_leave_them_seeing_both(self):
        """Two User Permissions widen rather than replace — which is Frappe's
        own semantics and the wrong one for "move this person to Colombo"."""
        other = self.second_branch()
        self.as_user(self.owner)
        api.assign_manager(user=self.manager, restaurant=self.home)
        api.assign_manager(user=self.manager, restaurant=other)
        self.assertEqual(branch.assigned(self.manager), [other])

    def test_the_setup_list_shows_what_each_branch_is_wired_to(self):
        other = self.second_branch()
        till = frappe.db.get_value("POS Profile", {}, "name")
        self.as_user(self.owner)
        api.assign_till(pos_profile=till, restaurant=other)
        self.addCleanup(frappe.db.set_value, "POS Profile", till, branch.FIELD, None)
        api.assign_manager(user=self.manager, restaurant=other)
        row = next(b for b in api.get_branch_setup()["branches"]
                   if b["restaurant"] == other)
        self.assertIn(till, row["tills"])
        self.assertIn(self.manager, row["managers"])

    def test_an_owner_is_never_offered_as_a_manager_to_restrict(self):
        """The absence of a User Permission is what makes somebody an Owner, so
        restricting one to a branch quietly demotes them."""
        self.as_user(self.owner)
        self.assertNotIn(self.owner, api.get_branch_setup()["managers"])
        self.assertIn(self.manager, api.get_branch_setup()["managers"])
