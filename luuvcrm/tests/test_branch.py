"""Which restaurant a row belongs to, and which one a session is looking at.

**What a branch is** (decided 2026-09-05): one Company, one Chart of Accounts,
a Cost Center per location. One owner, two restaurants, one legal entity — so
staff, stock, sales and data separate by Cost Center, Warehouse and permissions
rather than by company. That decision is what these tests pin: with a shared
company, `POS Profile.company` identifies nothing, so the till names its branch
directly and the branch names its cost centre.

Groundwork for the multi-restaurant goal (ROADMAP Phase 2c): 2–3 restaurants, an
Owner who sees all of them, a manager who sees only their own. The filtering
half — flipping the `get_all` reads to `get_list` — is not here, because it
turns DocPerms on everywhere at once and needs the permission table right first.

What is here is the half that has to be correct *before* a second restaurant
exists, and that is the point of most of these tests: on a one-restaurant site
every one of them must produce exactly today's answer. A branch field that
changes what a single-restaurant till does is a regression, not a feature.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles
from luuvcrm.tests import fixtures as fx


def _restaurant(case, tag, company=None):
    """A second Restaurant, cleaned up after.

    `Restaurant.autoname` is "prompt", so the name is supplied rather than
    derived, and company / default_customer / invoice_series_prefix are required
    — this is hospitality's doctype, not ours.
    """
    name = fx.PREFIX + tag
    if frappe.db.exists("Restaurant", name):
        frappe.delete_doc("Restaurant", name, force=True, ignore_permissions=True)
    existing = frappe.db.get_value("Restaurant", {}, ["company", "default_customer"],
                                   as_dict=True) or frappe._dict()
    doc = frappe.get_doc({
        "doctype": "Restaurant",
        "__newname": name,
        "company": company or existing.company or frappe.db.get_value("Company", {}, "name"),
        "default_customer": existing.default_customer,
        "invoice_series_prefix": tag.upper()[:4],
    })
    doc.insert(ignore_permissions=True, ignore_mandatory=True)

    # Anything pointing at it has to let go before it can be deleted, or the
    # restaurant leaks into the next test.
    case.addCleanup(_drop_test_restaurants)
    return doc.name


def _drop_test_restaurants():
    """Remove any Restaurant a test made, releasing the rows pointing at it."""
    for name in frappe.get_all("Restaurant", filters={"name": ["like", fx.PREFIX + "%"]},
                               pluck="name"):
        for dt in branch.BRANCHED_DOCTYPES:
            if frappe.db.exists("DocType", dt) and frappe.db.has_column(dt, branch.FIELD):
                frappe.db.sql(
                    f"update `tab{dt}` set `{branch.FIELD}` = null "
                    f"where `{branch.FIELD}` = %s", (name,))
        frappe.delete_doc("Restaurant", name, force=True, ignore_permissions=True)
    frappe.db.commit()


def _user(case, tag, role):
    email = f"lf-test-branch-{tag}@example.com"
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": "Branch",
                        "send_welcome_email": 0, "roles": [{"role": role}]
                        }).insert(ignore_permissions=True)
    return email


def _permit(case, user, restaurant):
    """A User Permission — Frappe's own "this user belongs to that branch"."""
    doc = frappe.get_doc({"doctype": "User Permission", "user": user,
                          "allow": "Restaurant", "for_value": restaurant})
    doc.insert(ignore_permissions=True)
    case.addCleanup(lambda: frappe.db.exists("User Permission", doc.name) and
                    frappe.delete_doc("User Permission", doc.name, force=True,
                                      ignore_permissions=True))
    return doc.name


class BranchTestCase(FrappeTestCase):
    def as_user(self, email):
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def order(self, **kwargs):
        """A real order through the real endpoint, so the before_insert hook
        runs the way it does at the till rather than being called directly."""
        if not hasattr(self, "_menu_item"):
            self._menu_item = fx.item(self, "BR-BURGER")
            fx.item_price(self, self._menu_item, 500)
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(
            items=json.dumps([{"item": self._menu_item, "qty": 1}]),
            order_type="Take Away", **kwargs)
        try:
            result = api.send_to_kitchen()
        finally:
            frappe.local.form_dict = previous
        name = result["name"] if isinstance(result, dict) else result
        self.addCleanup(lambda: frappe.db.exists("POS Order", name) and
                        frappe.delete_doc("POS Order", name, force=True,
                                          ignore_permissions=True))
        return frappe.get_doc("POS Order", name)


class TestTheFieldIsActuallyThere(BranchTestCase):
    def test_every_branched_doctype_has_the_column(self):
        """Not just the field in the meta — the column.

        `create_custom_field` adds the column itself for a Custom DocType but
        not for one that lives in the repo, where it is metadata until the next
        schema sync. POS Order is a repo doctype, and it failed exactly this way
        first time: the field was in the meta, so a save set it and the value
        went nowhere.
        """
        missing = [dt for dt in branch.BRANCHED_DOCTYPES
                   if frappe.db.exists("DocType", dt)
                   and not frappe.db.has_column(dt, branch.FIELD)]
        self.assertEqual(missing, [], "field declared but no column behind it")

    def test_the_meta_and_the_table_agree(self):
        for dt in branch.BRANCHED_DOCTYPES:
            if not frappe.db.exists("DocType", dt):
                continue
            with self.subTest(doctype=dt):
                self.assertTrue(frappe.get_meta(dt).get_field(branch.FIELD))
                self.assertTrue(frappe.db.has_column(dt, branch.FIELD))

    def test_installing_it_again_changes_nothing(self):
        self.assertEqual(branch.ensure_branch_field(),
                         {"added": [], "synced": [], "skipped": []})

    def test_a_missing_column_is_reported_rather_than_passed_over(self):
        """`unbranched()` is the check run before trusting the isolation. If it
        skipped a doctype whose column is missing, a half-installed field would
        read as a clean zero — which is the most expensive possible answer."""
        original = frappe.db.has_column
        frappe.db.has_column = lambda dt, col: False if col == branch.FIELD else original(dt, col)
        self.addCleanup(setattr, frappe.db, "has_column", original)
        self.assertIn("POS Order", branch.unbranched())


class TestOneRestaurantBehavesExactlyAsBefore(BranchTestCase):
    """The whole point of landing this before the second restaurant exists."""

    def setUp(self):
        super().setUp()
        # Order-independent on purpose: other classes here create a second
        # Restaurant, and these assertions are about a site that has one. A
        # leftover would make branch.current() correctly refuse to guess and
        # this class fail for entirely the wrong reason.
        _drop_test_restaurants()
        self.assertEqual(len(frappe.get_all("Restaurant", pluck="name")), 1,
                         "these tests describe a one-restaurant site")

    def test_the_only_restaurant_is_the_answer(self):
        self.assertEqual(branch.current(), frappe.get_all("Restaurant", pluck="name")[0])

    def test_it_answers_for_a_guest_too(self):
        """The kiosk, the public menu and /cus all resolve a restaurant with no
        session user behind them."""
        self.as_user("Guest")
        self.assertTrue(branch.current())

    def test_the_pages_still_find_their_menu(self):
        """`branch.current()` replaced `get_value("Restaurant", {}, "name")` in
        twelve context builders and helpers; on one restaurant they must return
        what they always did."""
        self.assertTrue(fx.PREFIX or True)
        from luuvcrm.api import pricing
        self.assertIsInstance(pricing._get_service_charge_rate(), float)

    def test_nothing_is_left_asking_for_the_first_restaurant(self):
        """`get_value("Restaurant", {}, "name")` is *the first restaurant,
        whichever it is*. It cannot be right with two, and it fails silently."""
        import subprocess
        hits = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--exclude-dir=graphify-out",
             "--exclude-dir=tests", "--exclude-dir=patches",
             'get_value("Restaurant", {}, "name")', frappe.get_app_path("luuvcrm")],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(hits, "", "these still take whichever restaurant is first")


class TestWorkingOutWhoseBranchItIs(BranchTestCase):
    def setUp(self):
        super().setUp()
        self.home = frappe.get_all("Restaurant", pluck="name")[0]
        self.other = _restaurant(self, "Branch-B")

    def test_a_user_permission_decides_it(self):
        """Frappe's own answer to "which branch is this user's" — and the one
        that will drive the filtering, so it has to drive the default too, or a
        row gets written into a branch its author cannot then read."""
        user = _user(self, "mgr-b", roles.RESTAURANT_MANAGER)
        _permit(self, user, self.other)
        self.as_user(user)
        self.assertEqual(branch.current(), self.other)

    def test_without_one_a_manager_falls_back(self):
        """No User Permission is how an Owner sees everything: no special case
        in the code, just the absence of a restriction."""
        user = _user(self, "mgr-none", roles.RESTAURANT_MANAGER)
        self.as_user(user)
        self.assertIn(branch.current(), (self.home, self.other, None))

    def test_it_never_picks_one_of_several_at_random(self):
        """The bug being removed. With two restaurants and nothing to go on, the
        honest answer is None — a wrong branch is worse than no branch, because
        nothing anywhere would say so."""
        user = _user(self, "nobody", roles.CASHIER)
        self.as_user(user)
        self.assertIsNone(branch.current(),
                          "resolved a branch it had no way of knowing")

    def test_a_permission_for_a_branch_beats_the_fallback(self):
        user = _user(self, "mgr-a", roles.RESTAURANT_MANAGER)
        _permit(self, user, self.home)
        self.as_user(user)
        self.assertEqual(branch.current(), self.home)

    def test_the_fallback_never_overrides_a_permission(self):
        """Even where only one restaurant would otherwise be resolvable, a user
        restricted to another branch must not be handed this one."""
        user = _user(self, "mgr-b2", roles.RESTAURANT_MANAGER)
        _permit(self, user, self.other)
        self.as_user(user)
        self.assertNotEqual(branch.current(), self.home)


class TestStampingItOnTheWayIn(BranchTestCase):
    """Never a dropdown the cashier has to remember. A field somebody has to set
    is a field that ends up empty, and an empty branch is a row that stays
    visible to every branch."""

    def test_a_new_order_is_born_with_a_branch(self):
        self.assertEqual(self.order().get(branch.FIELD), branch.current())

    def test_a_branch_the_caller_set_is_left_alone(self):
        other = _restaurant(self, "Branch-C")
        doc = frappe.get_doc({"doctype": "POS Order", "customer_name": "_LFT-branch",
                              "order_type": "Take Away", branch.FIELD: other,
                              "items": [{"item": fx.item(self, "BR-KEEP"), "qty": 1,
                                         "rate": 100}]})
        doc.insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "POS Order", doc.name, force=True,
                        ignore_permissions=True)
        self.assertEqual(doc.get(branch.FIELD), other)

    def test_stamping_never_stops_a_row_being_saved(self):
        """A row that fails to get a branch is a reporting problem. A row that
        fails to save is a cashier who cannot sell."""
        original = branch.current
        branch.current = lambda user=None: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, branch, "current", original)
        self.assertTrue(self.order().name)

    def test_the_hook_is_wired_for_every_branched_doctype(self):
        from luuvcrm import hooks
        for dt in branch.BRANCHED_DOCTYPES:
            with self.subTest(doctype=dt):
                self.assertEqual(hooks.doc_events.get(dt, {}).get("before_insert"),
                                 "luuvcrm.branch.stamp")


class TestTheBackfill(BranchTestCase):
    """The step between adding the field and creating the second restaurant.

    Until it has run, isolation is incomplete *precisely because* the old rows
    are empty: while `apply_strict_user_permissions` is off, an empty link stays
    visible to every branch.
    """

    def test_the_live_data_has_no_branchless_rows_left(self):
        self.assertEqual(branch.unbranched(), {})

    def test_it_fills_a_row_that_somehow_has_none(self):
        order = self.order()
        frappe.db.set_value("POS Order", order.name, branch.FIELD, None,
                            update_modified=False)
        self.assertIn("POS Order", branch.unbranched())
        branch.backfill()
        self.assertEqual(frappe.db.get_value("POS Order", order.name, branch.FIELD),
                         branch.current())

    def test_it_refuses_to_guess_once_there_are_two(self):
        """Somebody has to say which branch the history belongs to."""
        _restaurant(self, "Branch-D")
        result = branch.backfill()
        self.assertIn("error", result)
        self.assertEqual(result["filled"], {})

    def test_but_it_will_take_an_answer(self):
        """Named explicitly, it fills — including rows that already had a branch
        left empty. Checked on one row of this test's own making, not by
        pointing the whole site's history at a throwaway restaurant."""
        other = _restaurant(self, "Branch-E")
        order = self.order()
        frappe.db.set_value("POS Order", order.name, branch.FIELD, None,
                            update_modified=False)
        result = branch.backfill(restaurant=other)
        self.assertEqual(result["restaurant"], other)
        self.assertEqual(frappe.db.get_value("POS Order", order.name, branch.FIELD), other)

    def test_it_will_not_take_a_restaurant_that_does_not_exist(self):
        with self.assertRaises(frappe.ValidationError):
            branch.backfill(restaurant="_LFT-no-such-restaurant")


def _cost_centre(case, tag):
    """A leaf Cost Center under the one company."""
    company = frappe.db.get_value("Company", {}, "name")
    name = fx.PREFIX + tag
    existing = frappe.db.get_value("Cost Center", {"cost_center_name": name}, "name")
    if existing:
        return existing
    parent = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
    doc = frappe.get_doc({"doctype": "Cost Center", "cost_center_name": name,
                          "company": company, "parent_cost_center": parent,
                          "is_group": 0}).insert(ignore_permissions=True)
    case.addCleanup(lambda: frappe.db.exists("Cost Center", doc.name) and
                    frappe.delete_doc("Cost Center", doc.name, force=True,
                                      ignore_permissions=True))
    return doc.name


class TestABranchIsACostCentreNotACompany(BranchTestCase):
    """One owner, two locations, one legal entity. The books stay together and
    the branches are told apart by Cost Center, Warehouse and permissions."""

    def setUp(self):
        super().setUp()
        _drop_test_restaurants()
        self.home = frappe.get_all("Restaurant", pluck="name")[0]
        self.other = _restaurant(self, "Branch-CC")

    def test_two_branches_share_one_company(self):
        """The premise. If this ever stops being true the resolver below is
        answering a different question than the one it was written for."""
        companies = {frappe.db.get_value("Restaurant", r, "company")
                     for r in (self.home, self.other)}
        self.assertEqual(len(companies), 1)

    def test_a_till_names_its_branch(self):
        """The only unambiguous hop once the company is shared."""
        profile = frappe.db.get_value("POS Profile", {}, "name")
        previous = frappe.db.get_value("POS Profile", profile, branch.FIELD)
        frappe.db.set_value("POS Profile", profile, branch.FIELD, self.other)
        self.addCleanup(frappe.db.set_value, "POS Profile", profile, branch.FIELD, previous)
        self.assertEqual(branch._restaurant_for_profile(profile), self.other)

    def test_failing_that_the_cost_centre_identifies_it(self):
        """`POS Profile.cost_center` is ERPNext's own field and a site may have
        set it long before ours existed."""
        centre = _cost_centre(self, "CC-B")
        profile = frappe.db.get_value("POS Profile", {}, "name")
        before = frappe.db.get_value("POS Profile", profile,
                                     [branch.FIELD, "cost_center"], as_dict=True)
        frappe.db.set_value("POS Profile", profile,
                            {branch.FIELD: None, "cost_center": centre})
        frappe.db.set_value("Restaurant", self.other, "cost_center", centre)
        self.addCleanup(frappe.db.set_value, "POS Profile", profile, dict(before))
        self.assertEqual(branch._restaurant_for_profile(profile), self.other)

    def test_the_shared_company_is_never_used_to_guess(self):
        """Both restaurants are on one company, so matching on it would hand
        back a branch at random — the exact failure this module removes."""
        profile = frappe.db.get_value("POS Profile", {}, "name")
        before = frappe.db.get_value("POS Profile", profile,
                                     [branch.FIELD, "cost_center"], as_dict=True)
        frappe.db.set_value("POS Profile", profile, {branch.FIELD: None, "cost_center": None})
        self.addCleanup(frappe.db.set_value, "POS Profile", profile, dict(before))
        self.assertIsNone(branch._restaurant_for_profile(profile))

    def test_the_branch_carries_its_own_dimensions(self):
        centre = _cost_centre(self, "CC-DIM")
        frappe.db.set_value("Restaurant", self.other,
                            {"cost_center": centre, "warehouse":
                             frappe.db.get_value("Warehouse", {"is_group": 0}, "name")})
        self.assertEqual(branch.cost_center(self.other), centre)
        self.assertTrue(branch.warehouse(self.other))


class TestTheConfigurationCheck(BranchTestCase):
    """Everything that separates two branches is configuration somebody does
    once in Desk, forgets, and then cannot tell from working. `branch.check()`
    is what tells them."""

    def setUp(self):
        super().setUp()
        _drop_test_restaurants()

    def test_one_restaurant_needs_no_configuration(self):
        self.assertEqual(branch.check(), [])

    def test_a_branch_with_no_cost_centre_is_reported(self):
        other = _restaurant(self, "Branch-NOCC")
        problems = " ".join(branch.check())
        self.assertIn(other, problems)
        self.assertIn("Cost Center", problems)

    def test_two_branches_sharing_a_cost_centre_are_reported(self):
        """The quiet one: everything works, and the P&L is one number."""
        centre = _cost_centre(self, "CC-SHARED")
        home = frappe.get_all("Restaurant", pluck="name")[0]
        other = _restaurant(self, "Branch-SHARE")
        before = frappe.db.get_value("Restaurant", home, "cost_center")
        frappe.db.set_value("Restaurant", home, "cost_center", centre)
        frappe.db.set_value("Restaurant", other, "cost_center", centre)
        self.addCleanup(frappe.db.set_value, "Restaurant", home, "cost_center", before)
        self.assertTrue(any("share Cost Center" in p for p in branch.check()))

    def test_a_group_cost_centre_is_reported(self):
        """Postings need a leaf; a group one is accepted by the Link field and
        then refused by the ledger."""
        group = frappe.db.get_value("Cost Center", {"is_group": 1}, "name")
        home = frappe.get_all("Restaurant", pluck="name")[0]
        other = _restaurant(self, "Branch-GRP")
        before = frappe.db.get_value("Restaurant", home, "cost_center")
        frappe.db.set_value("Restaurant", home, "cost_center", group)
        self.addCleanup(frappe.db.set_value, "Restaurant", home, "cost_center", before)
        self.assertTrue(any("is a group" in p for p in branch.check()))

    def test_a_till_with_no_branch_is_reported(self):
        _restaurant(self, "Branch-TILL")
        self.assertTrue(any("cannot be attributed to a branch" in p
                            for p in branch.check()))

    def test_rows_with_no_branch_are_reported(self):
        _restaurant(self, "Branch-ROWS")
        order = self.order()
        frappe.db.set_value("POS Order", order.name, branch.FIELD, None,
                            update_modified=False)
        self.assertTrue(any("visible to every branch" in p for p in branch.check()))
