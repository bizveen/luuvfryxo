from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import branch


class TestOperationalBranchSwitcher(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.user = "Administrator"
        self.previous_user = frappe.session.user
        frappe.set_user(self.user)
        self.previous_choice = branch.selected(self.user)
        self.addCleanup(self._restore)

    def _restore(self):
        branch.select(self.previous_choice or "", self.user)
        frappe.set_user(self.previous_user)

    def test_every_operational_app_has_the_shared_switcher(self):
        root = Path(frappe.get_app_path("luuvcrm")) / "www"
        for page in ("manager", "pos", "waiter", "kitchen", "purchases"):
            source = (root / f"{page}.html").read_text()
            with self.subTest(page=page):
                self.assertIn('data-active-restaurant', source)
                self.assertIn('templates/includes/branch_switcher.html', source)

    def test_an_explicit_choice_scopes_reports_and_pos_profiles(self):
        restaurants = branch.all_restaurants()
        if len(restaurants) < 2:
            self.skipTest("branch switching needs two restaurants")

        for restaurant in restaurants[:2]:
            branch.select(restaurant, self.user)
            with self.subTest(restaurant=restaurant):
                self.assertEqual(branch.viewing(), restaurant)
                self.assertEqual(branch._scoped_to(), restaurant)
                self.assertEqual(branch.sql_restaurant("x")[1], (restaurant,))
                centre = branch.cost_center(restaurant)
                if centre:
                    self.assertEqual(branch.invoice_filters(), {"cost_center": centre})
                for profile in branch.accessible_pos_profiles():
                    self.assertEqual(branch._restaurant_for_profile(profile), restaurant)

    def test_choice_endpoint_returns_only_available_restaurants(self):
        from luuvcrm.api.owner import get_branch_choice

        result = get_branch_choice()
        self.assertEqual(result["restaurants"], branch.available_restaurants())
        self.assertEqual(result["can_choose"], len(result["restaurants"]) > 1)
        self.assertEqual(result["viewing_label"], branch.display_label(result["viewing"]))
        self.assertEqual(result["restaurant_labels"], {
            name: branch.display_label(name) for name in result["restaurants"]
        })

    def test_the_sheet_shows_display_name_and_location_without_changing_the_id(self):
        source = (Path(frappe.get_app_path("luuvcrm")) /
                  "templates" / "includes" / "branch_switcher.html").read_text()
        self.assertIn("choice.restaurant_labels?.[restaurant] || restaurant", source)
        self.assertIn("selectRestaurant(restaurant)", source)

