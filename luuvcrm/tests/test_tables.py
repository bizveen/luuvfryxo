"""The tables people sit at — adding one, and taking one out of service.

Before this a table could only be created in the Frappe Desk, and could not be
stopped at all: `Restaurant Table` is hospitality's and carries no flag. So a
corner that had become a service station stayed on the POS grid, the waiter's
screen, the kiosk and the printed QR sheet for ever, and the only way to remove
one was to delete it — which either fails or orphans every bill that links to it
by name.

`enabled` is ours (setup_doctype.CUSTOM_FIELDS), default 1, and it means the
same as the menu's `available`: hidden from the pickers, and never allowed to
touch a bill. These tests are mostly about the second half. A restaurant turns a
table off between services; a flag that could strand a party who have eaten and
want to pay is worse than no flag at all.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, branch, roles
from luuvcrm.api import tables
from luuvcrm.tests import fixtures as fx


class TableTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        if not tables._has_flag():
            self.skipTest("this site has no Restaurant Table.enabled column yet")

    def table(self, tag, restaurant=None, seats=4):
        name = fx.PREFIX + tag
        if frappe.db.exists("Restaurant Table", name):
            frappe.delete_doc("Restaurant Table", name, force=True, ignore_permissions=True)
        doc = frappe.get_doc({"doctype": "Restaurant Table", "__newname": name,
                              "restaurant": restaurant or branch.viewing(),
                              "no_of_seats": seats, "enabled": 1})
        doc.insert(ignore_permissions=True, ignore_mandatory=True)
        self.addCleanup(self._drop, doc.name)
        return doc.name

    def _drop(self, name):
        for order in frappe.get_all("POS Order", filters={"restaurant_table": name}, pluck="name"):
            frappe.db.set_value("POS Order", order, "restaurant_table", None)
        if frappe.db.exists("Restaurant Table", name):
            frappe.delete_doc("Restaurant Table", name, force=True, ignore_permissions=True)

    def picker(self):
        return [t["name"] for t in tables.for_picker()]


class TestTurningATableOff(TableTestCase):
    def test_a_new_table_is_in_service(self):
        name = self.table("T-NEW")
        self.assertIn(name, self.picker())
        self.assertTrue(tables.in_service(name))

    def test_turning_it_off_takes_it_off_every_picker(self):
        """One reader behind all of them, so it cannot be true on the POS and
        false on the kiosk — which is how the menu's availability check ended up
        copied into eight files, five of them byte-identical."""
        name = self.table("T-OFF")
        api.set_table_enabled(table=name, enabled=0)
        self.assertNotIn(name, self.picker())
        self.assertNotIn(name, [t["name"] for t in api.get_tables()["tables"]])
        self.assertNotIn(name, [t["name"] for t in api.get_menu_data()["tables"]])

    def test_and_putting_it_back_returns_it(self):
        name = self.table("T-BACK")
        api.set_table_enabled(table=name, enabled=0)
        api.set_table_enabled(table=name, enabled=1)
        self.assertIn(name, self.picker())

    def test_nothing_is_deleted(self):
        """Every bill links to its table by name. Off is a flag, never a delete."""
        name = self.table("T-KEEP")
        api.set_table_enabled(table=name, enabled=0)
        self.assertTrue(frappe.db.exists("Restaurant Table", name))

    def test_a_disabled_table_refuses_a_new_order(self):
        name = self.table("T-SHUT")
        api.set_table_enabled(table=name, enabled=0)
        self.assertFalse(tables.in_service(name))

    def test_but_the_bill_already_on_it_is_untouched(self):
        """The promise the whole flag rests on. A table is turned off between
        services; the party who ordered before it was still have to pay."""
        name = self.table("T-BUSY")
        item = fx.item(self, "TBL-BURGER")
        fx.item_price(self, item, 400)
        order = _order_on(self, name, item)

        api.set_table_enabled(table=name, enabled=0)

        doc = frappe.get_doc("POS Order", order)
        self.assertEqual(doc.restaurant_table, name)
        self.assertGreater(float(doc.grand_total or 0), 0,
                           "an order on a table that has been turned off still has a total")
        still = api.get_table_orders.__wrapped__ if hasattr(api.get_table_orders, "__wrapped__") \
            else api.get_table_orders
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(table=name)
        try:
            found = [o["name"] for o in still()["orders"]]
        finally:
            frappe.local.form_dict = previous
        self.assertIn(order, found, "the bill has to still be reachable from the table")

    def test_the_screen_is_told_what_is_sitting_on_it(self):
        """Turning off a table with a bill on it is allowed — but the person
        doing it should see that it will happen, not find out at closing."""
        name = self.table("T-COUNT")
        item = fx.item(self, "TBL-COUNT")
        fx.item_price(self, item, 300)
        _order_on(self, name, item)
        out = api.set_table_enabled(table=name, enabled=0)
        self.assertEqual(out["open_orders"], 1)


class TestAddingATable(TableTestCase):
    def test_a_table_can_be_added_without_the_desk(self):
        out = api.add_table(seats=6)
        self.addCleanup(self._drop, out["table"])
        self.assertTrue(frappe.db.exists("Restaurant Table", out["table"]))
        self.assertEqual(frappe.db.get_value("Restaurant Table", out["table"], "no_of_seats"), 6)
        self.assertEqual(frappe.db.get_value("Restaurant Table", out["table"], "enabled"), 1)
        self.assertIn(out["table"], self.picker())

    def test_the_name_comes_from_the_branch_not_from_the_caller(self):
        """hospitality's autoname builds it — `New-Rest-04` — and the screens
        show the last segment, which is the number the floor already uses. A
        name field on the screen would have been thrown away in silence."""
        here = branch.viewing()
        out = api.add_table(seats=2, restaurant=here)
        self.addCleanup(self._drop, out["table"])
        self.assertTrue(out["table"].startswith(here.replace(" ", "-")),
                        "%s does not name its branch" % out["table"])

    def test_the_numbers_keep_going_up(self):
        first = api.add_table(seats=2)
        self.addCleanup(self._drop, first["table"])
        second = api.add_table(seats=2)
        self.addCleanup(self._drop, second["table"])
        self.assertNotEqual(first["table"], second["table"])

    def test_a_branch_is_not_optional(self):
        """hospitality's autoname dereferences `restaurant` without checking it,
        so without one this fails inside hospitality with nothing a person could
        act on. Refuse it here, in words."""
        with self.assertRaises(frappe.ValidationError):
            api.add_table(seats=2, restaurant="_LFT-no-such-branch")


class TestTablesBelongToABranch(TableTestCase):
    def test_the_setup_screen_reports_what_is_open(self):
        name = self.table("T-SETUP")
        out = api.get_tables_setup()
        row = next((t for t in out["tables"] if t["name"] == name), None)
        self.assertIsNotNone(row, "a table of this branch should be on the branch's screen")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["open_orders"], 0)
        self.assertTrue(out["can_toggle"])

    def test_the_pickers_no_longer_read_the_doctype_themselves(self):
        """A grep, in the spirit of the one guarding `get_value("Restaurant")`.
        A screen that builds its own table list is a screen that will still be
        offering a table nobody sits at, and nothing would say so."""
        import pathlib
        import subprocess

        root = pathlib.Path(branch.__file__).parent
        hits = subprocess.run(
            ["grep", "-rn", '--include=*.py',
             'get_all("Restaurant Table"', str(root)],
            capture_output=True, text=True).stdout
        allowed = ("/api/tables.py", "/tests/")
        stray = [line for line in hits.splitlines()
                 if line.strip() and not any(a in line for a in allowed)]
        self.assertEqual(stray, [], "these still build their own table list")


def _order_on(case, table, item):
    """A real order through the real endpoint, so it is shaped the way the till
    shapes one rather than the way a test would."""
    import json

    previous = frappe.local.form_dict
    frappe.local.form_dict = frappe._dict(
        items=json.dumps([{"item": item, "qty": 1}]),
        order_type="Dine In", table=table)
    try:
        result = api.send_to_kitchen()
    finally:
        frappe.local.form_dict = previous
    name = result["name"] if isinstance(result, dict) else result
    case.addCleanup(lambda: frappe.db.exists("POS Order", name) and
                    frappe.delete_doc("POS Order", name, force=True, ignore_permissions=True))
    return name
