import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from luuvcrm.www import home


class TestRestaurantLabels(unittest.TestCase):
    def test_brand_and_city_do_not_change_branch_routing(self):
        records = {
            "Luuv Fryxo": frappe._dict(restaurant_name="Rangiri Luuv", city="Dambulla"),
            "Kurunegala": frappe._dict(restaurant_name="Luuv Fryxo", city="Kurunegala"),
        }
        with patch.object(frappe, "get_cached_doc", side_effect=lambda dt, name: records[name]), \
                patch.object(frappe.db, "get_value", return_value=None):
            for branch_id, record in records.items():
                row = home._location(branch_id, deep_link={"ref": "EXAMPLE"})
                self.assertEqual(row.display_name, record.restaurant_name)
                self.assertEqual(row.city, record.city)
                self.assertEqual(row.label, f"{record.restaurant_name} — {record.city}")
                self.assertEqual(row.address, f"{record.city}, Sri Lanka")
                for url in (row.order_url, row.menu_url, row.rewards_url):
                    query = parse_qs(urlsplit(url).query)
                    self.assertEqual(query["r"], [branch_id])
                    self.assertEqual(query["ref"], ["EXAMPLE"])

    def test_unconfigured_restaurant_keeps_its_name(self):
        with patch.object(frappe, "get_cached_doc", return_value=frappe._dict()), \
                patch.object(frappe.db, "get_value", return_value=None):
            row = home._location("Other Restaurant")
        self.assertEqual(row.display_name, "Other Restaurant")
        self.assertEqual(row.label, "Other Restaurant")
        self.assertEqual(row.city, "")
