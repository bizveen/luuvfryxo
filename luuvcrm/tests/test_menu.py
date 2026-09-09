"""The menu: one reader, and the owner's editor on top of it.

**Why one reader.** Eight screens build the menu — `/pos`, `/waiter`, `/kiosk`,
`/order`, `/menu`, `/home`, `/cus` and the POS's offline payload — and each had
its own copy of the same loop, five of them byte-identical. That was survivable
while the only question was "what is on the menu". It stops being survivable the
moment there is a rule: an item switched off has to vanish from *all eight*, and
a rule written eight times holds in seven places and fails quietly in the
eighth. That is the exact shape of every bug found on 2026-09-07, so the
availability flag went in behind a single `menu_items()` and these tests check
each screen through it.

`available` is deliberately a menu-row flag and not `Item.disabled`: it is
reversible with one tap, and it must not stop a bill that already carries the
item from being priced or paid. Hiding it from the ordering screens is the whole
effect, and one of the tests below is there to keep it that way.
"""

import importlib
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from luuvcrm import api, roles
from luuvcrm.api import menu as M
from luuvcrm.tests import fixtures as fx


#: Page modules. `/order` and `/home` share their names with erpnext pages and
#: are ours by install order — see test_pages.TestNoPageIsShadowedByAnotherApp.
PAGES = ("pos", "waiter", "kiosk", "order", "menu", "home", "cus")


class MenuTestCase(FrappeTestCase):
    def setUp(self):
        super().setUp()
        self.restaurant = frappe.db.get_value("Restaurant", {}, "name")
        if not self.restaurant:
            self.skipTest("no Restaurant on this site")
        self.menu = M.active_menu_name(self.restaurant)
        if not self.menu:
            self.skipTest("the restaurant has no active menu")
        self.burger = fx.item(self, "MENU-BURGER")
        fx.item_price(self, self.burger, 900)
        self.on_menu(self.burger, 900)

    def on_menu(self, code, rate):
        """Put an item on the active menu for the duration of one test.

        Through the same helper the app uses, deliberately: saving the parent
        `Restaurant Menu` fires hospitality's `on_update`, which rebuilds the
        menu's whole Price List and throws outright on a site whose Price List
        it did not create. A fixture that avoids the app's real path would hide
        exactly the bug this helper exists to route around.
        """
        doc = frappe.get_doc("Restaurant Menu", self.menu)
        if not any(r.item == code for r in doc.items):
            M._append_menu_row(doc, code, rate)
        row = frappe.db.get_value("Restaurant Menu Item",
                                  {"parent": self.menu, "item": code}, "name")
        self.addCleanup(self._drop_row, row)
        return row

    def _drop_row(self, row):
        if row and frappe.db.exists("Restaurant Menu Item", row):
            frappe.db.delete("Restaurant Menu Item", {"name": row})

    def call(self, endpoint, **kwargs):
        previous = frappe.local.form_dict
        frappe.local.form_dict = frappe._dict(kwargs)
        try:
            return endpoint(**kwargs) if kwargs else endpoint()
        finally:
            frappe.local.form_dict = previous

    def _restaurant_fields(self):
        """hospitality's Restaurant wants a company, a default customer and an
        invoice series before it will save. Copied from the site's existing one
        so the fixture does not invent a chart of accounts."""
        like = frappe.db.get_value("Restaurant", self.restaurant,
                                   ["company", "default_customer", "invoice_series_prefix"],
                                   as_dict=True) or frappe._dict()
        return {
            "doctype": "Restaurant",
            "company": like.get("company") or frappe.db.get_value("Company", {}, "name"),
            "default_customer": like.get("default_customer")
                                or frappe.db.get_value("Customer", {}, "name"),
            "invoice_series_prefix": "LFT-",
        }

    def codes(self, **kwargs):
        return [i["code"] for i in M.menu_items(self.restaurant, **kwargs)]

    def page_codes(self, page):
        """Every item a page's context builder would render."""
        module = importlib.import_module(f"luuvcrm.www.{page}")
        ctx = frappe._dict()
        try:
            module.get_context(ctx)
        except frappe.Redirect:
            self.skipTest(f"/{page} redirected")
        if ctx.get("menu_items") is not None:
            return [i["code"] for i in ctx.menu_items]
        return [i["code"] for g in (ctx.get("menu_groups") or []) for i in g["items"]]


class TestOneReader(MenuTestCase):
    def test_every_screen_reads_the_menu_through_one_function(self):
        """The copies are what this replaces. A page building its own loop again
        is a page the next menu rule will not reach."""
        import pathlib

        www = pathlib.Path(frappe.get_app_path("luuvcrm", "www"))
        offenders = []
        for page in PAGES:
            body = (www / f"{page}.py").read_text()
            if "for row in menu.items" in body or 'frappe.get_doc("Item"' in body:
                offenders.append(page)
        self.assertEqual(offenders, [],
                         f"these build the menu themselves again: {offenders}")

    def test_the_reader_does_not_build_a_document_per_item(self):
        built = []
        real = frappe.get_doc

        def counting(*args, **kwargs):
            if args and args[0] == "Item":
                built.append(args[0])
            return real(*args, **kwargs)

        frappe.get_doc = counting
        try:
            items = M.menu_items(self.restaurant)
        finally:
            frappe.get_doc = real
        self.assertTrue(items, "the fixture item should be on the menu")
        self.assertEqual(built, [], "the menu is one grouped read, not a document per row")

    def test_it_carries_what_the_screens_render(self):
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        for key in ("code", "name", "description", "image", "rate", "group",
                    "available", "kitchen_group", "prep_time", "tags", "youtube"):
            self.assertIn(key, item)

    def test_a_site_with_no_branch_gets_an_empty_menu_not_an_error(self):
        """Public pages have no session user, so this is the ordinary case the
        day there are two restaurants — a blank menu, never a 500."""
        self.assertEqual(M.menu_items("no-such-restaurant"), [])
        self.assertIsNone(M.active_menu_name("no-such-restaurant"))


class TestAMenuBelongsToARestaurant(MenuTestCase):
    """A `Restaurant Menu` names its restaurant, and `Restaurant.active_menu`
    picks which of them is live. Every screen resolves through that pair, so
    every editing call has to say *which restaurant* — with two branches
    `branch.current()` answers "the only one" and then None, which is the right
    refusal for a public page and useless for an editor.

    The failure this prevents is not an error: it is an owner repricing Kandy's
    burger while Colombo's list is on the screen.
    """

    def second_restaurant(self, name="_LFT-MENU-SECOND"):
        """Restaurant is prompt-named, so the name is set rather than derived."""
        if not frappe.db.exists("Restaurant", name):
            doc = frappe.get_doc(self._restaurant_fields())
            doc.name = name
            doc.insert(ignore_permissions=True)
        self.addCleanup(self._drop_restaurant, name)
        return name

    def _drop_restaurant(self, name):
        for menu in frappe.get_all("Restaurant Menu", filters={"restaurant": name}, pluck="name"):
            frappe.delete_doc("Restaurant Menu", menu, force=True, ignore_permissions=True)
        if frappe.db.exists("Restaurant", name):
            frappe.delete_doc("Restaurant", name, force=True, ignore_permissions=True)

    def card(self, restaurant, name):
        """A second Restaurant Menu. `items` is mandatory on the doctype, so it
        gets one — and saving the parent is what fires hospitality's price-list
        rebuild, which is why the app never saves one to add a row."""
        if not frappe.db.exists("Restaurant Menu", name):
            doc = frappe.get_doc({"doctype": "Restaurant Menu", "restaurant": restaurant,
                                  "enabled": 1,
                                  "items": [{"item": self.burger, "rate": 900}]})
            doc.name = name
            doc.insert(ignore_permissions=True)
        self.addCleanup(lambda: frappe.db.exists("Restaurant Menu", name) and
                        frappe.delete_doc("Restaurant Menu", name, force=True,
                                          ignore_permissions=True))
        return name

    def test_the_editor_answers_for_the_restaurant_it_was_asked_about(self):
        other = self.second_restaurant()
        admin = api.get_menu_admin(restaurant=other)
        self.assertEqual(admin["restaurant"], other)
        self.assertIn(self.restaurant, [r["name"] for r in admin["restaurants"]])

    def test_editing_names_the_restaurant_it_edits(self):
        """`_active_menu_doc(restaurant)` is what makes this work; without the
        argument the same call edits whichever menu branch.current() lands on."""
        other = self.second_restaurant()
        with self.assertRaises(frappe.ValidationError):
            # The fixture item is on *this* restaurant's menu, not the other's.
            api.update_menu_item(item_code=self.burger, rate=999, restaurant=other)
        api.update_menu_item(item_code=self.burger, rate=999, restaurant=self.restaurant)
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        self.assertEqual(item["rate"], 999)

    def test_the_item_master_asks_about_one_restaurant_s_menu(self):
        """"On the menu" is a question about a restaurant. Reading the wrong one
        tells an owner an item is off the menu because it looked at the other
        branch."""
        other = self.second_restaurant()
        here = api.get_items(search=self.burger, restaurant=self.restaurant)
        there = api.get_items(search=self.burger, restaurant=other)
        self.assertTrue(next(i for i in here["items"] if i["code"] == self.burger)["on_menu"])
        self.assertFalse(next(i for i in there["items"] if i["code"] == self.burger)["on_menu"])

    def test_the_item_master_offers_the_restaurants_to_pick_from(self):
        listing = api.get_items()
        self.assertIn("restaurants", listing)
        self.assertIn(self.restaurant, listing["restaurants"])


class TestWhichCardIsLive(MenuTestCase):
    """A restaurant can have several menus — a lunch card, a season, last
    year's. `Restaurant.active_menu` is the one the till, the kiosk, the public
    page and the offline payload all read, and until now it could only be
    changed in the Desk."""

    def second_menu(self, name=None, restaurant=None):
        """A second card for this restaurant. `items` is mandatory on the
        doctype, so it gets one — a different item from the fixture's, which is
        what makes "the old card's items are not on the new one" mean
        something."""
        restaurant = restaurant or self.restaurant
        # A fresh name per test. Creating a Restaurant Menu creates a Price List
        # named after it (hospitality's on_update), that insert outlives the
        # suite's rollback, and deleting the menu does not take the Price List
        # with it — so a fixed name collides with the previous test's leftover.
        name = name or "_LFT-CARD-%s" % frappe.generate_hash(length=6).upper()
        self.addCleanup(self._restore_card,
                        frappe.db.get_value("Restaurant", self.restaurant, "active_menu"))
        # Only the Price List is cleaned up by hand. **The menu is deliberately
        # left to the rollback**: deleting a Restaurant Menu fires hospitality's
        # on_trash → clear_item_price → get_price_list, and that helper *creates
        # the Price List again* when its lookup misses — which it always does,
        # because the `restaurant_menu` field it looks up by is not persisted on
        # the Price List it just made. Deleting the menu therefore leaves a new
        # Price List behind for the next test to collide with.
        self.addCleanup(self._drop_price_list, name)
        if not frappe.db.exists("Restaurant Menu", name):
            other = fx.item(self, "MENU-EVENING-ONLY")
            doc = frappe.get_doc({"doctype": "Restaurant Menu", "restaurant": restaurant,
                                  "enabled": 1, "items": [{"item": other, "rate": 600}]})
            doc.name = name
            doc.insert(ignore_permissions=True)
        return name

    def _drop_price_list(self, name):
        if frappe.db.exists("Price List", name):
            frappe.db.delete("Item Price", {"price_list": name})
            frappe.delete_doc("Price List", name, force=True, ignore_permissions=True)

    def _restore_card(self, was):
        if was:
            frappe.db.set_value("Restaurant", self.restaurant, "active_menu", was)

    def test_switching_the_card_changes_what_every_screen_reads(self):
        other = self.second_menu()
        api.set_active_menu(restaurant=self.restaurant, menu=other)
        self.assertEqual(M.active_menu_name(self.restaurant), other)
        self.assertNotIn(self.burger, self.codes(),
                         "the old card's items are not on the new one")

    def test_the_editor_lists_the_cards_it_could_switch_to(self):
        other = self.second_menu()
        names = [m["name"] for m in api.get_menu_admin(restaurant=self.restaurant)["menus"]]
        self.assertIn(other, names)
        self.assertIn(self.menu, names)

    def test_a_card_belonging_to_another_restaurant_is_refused(self):
        """Otherwise a branch quietly starts selling another branch's card."""
        other_restaurant = "_LFT-MENU-OTHER"
        if not frappe.db.exists("Restaurant", other_restaurant):
            doc = frappe.get_doc(self._restaurant_fields())
            doc.name = other_restaurant
            doc.insert(ignore_permissions=True)
        self.addCleanup(lambda: frappe.db.exists("Restaurant", other_restaurant) and
                        frappe.delete_doc("Restaurant", other_restaurant, force=True,
                                          ignore_permissions=True))
        name = self.second_menu(restaurant=other_restaurant)
        with self.assertRaises(frappe.ValidationError):
            api.set_active_menu(restaurant=self.restaurant, menu=name)

    def test_a_menu_that_does_not_exist_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.set_active_menu(restaurant=self.restaurant, menu="no-such-card")

    def test_a_cashier_cannot_swap_the_card(self):
        email = "lf-test-card-cashier@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Card Test",
                            "send_welcome_email": 0, "roles": [{"role": roles.CASHIER}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)
        with self.assertRaises(frappe.PermissionError):
            api.set_active_menu(restaurant=self.restaurant, menu=self.menu)


class TestSoldOut(MenuTestCase):
    def test_it_disappears_from_every_screen_at_once(self):
        self.assertIn(self.burger, self.codes())
        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        self.assertNotIn(self.burger, self.codes())
        for page in PAGES:
            with self.subTest(page=page):
                self.assertNotIn(self.burger, self.page_codes(page))

    def test_the_offline_payload_drops_it_too(self):
        """The till caches this to keep selling with no wifi. An item left in it
        is one the counter can still sell after the kitchen has run out."""
        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        codes = [i["code"] for i in api.get_menu_data()["items"]]
        self.assertNotIn(self.burger, codes)

    def test_the_editor_still_shows_it(self):
        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        self.assertIn(self.burger, self.codes(include_unavailable=True))
        admin = api.get_menu_admin(restaurant=self.restaurant)
        row = next(i for i in admin["items"] if i["code"] == self.burger)
        self.assertEqual(row["available"], 0)

    def test_a_bill_that_already_has_it_still_prices(self):
        """This is why it is a menu flag and not Item.disabled: the kitchen runs
        out at 8pm and the table that ordered at 7:55 still has to be paid."""
        from luuvcrm.api.pricing import _server_item_rate

        before = _server_item_rate(self.burger)
        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        self.assertEqual(_server_item_rate(self.burger), before)

    def test_turning_it_back_on_puts_it_back(self):
        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        api.set_menu_item_available(item_code=self.burger, available=1,
                                    restaurant=self.restaurant)
        self.assertIn(self.burger, self.codes())

    def test_the_switch_is_written_down(self):
        from luuvcrm import activity

        api.set_menu_item_available(item_code=self.burger, available=0,
                                    restaurant=self.restaurant)
        rows = frappe.db.sql(
            "select * from `tab{0}` where action = 'menu.available' and ref_name = %s"
            .format(activity.DOCTYPE), (self.burger,), as_dict=True)
        self.assertTrue(rows, "hiding an item from every screen is worth a row")


class TestTakingAnItemOffTheMenu(MenuTestCase):
    def test_remove_keeps_the_item_itself(self):
        """Every past invoice links to the Item. Deleting it would take the
        history with it, so remove only ever touches the menu row."""
        api.remove_menu_item(item_code=self.burger, restaurant=self.restaurant)
        self.assertNotIn(self.burger, self.codes(include_unavailable=True))
        self.assertTrue(frappe.db.exists("Item", self.burger))

    def test_restore_puts_it_back_at_its_price(self):
        api.remove_menu_item(item_code=self.burger, restaurant=self.restaurant)
        self.on_menu_cleanup = api.restore_menu_item(item_code=self.burger,
                                                     restaurant=self.restaurant)
        self.assertIn(self.burger, self.codes())
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        self.assertEqual(item["rate"], 900)

    def test_restoring_something_already_on_the_menu_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.restore_menu_item(item_code=self.burger, restaurant=self.restaurant)

    def test_the_editor_lists_what_is_off_the_menu(self):
        """Retyping the name instead creates a second Item with the same name
        and splits the item's history in two."""
        api.remove_menu_item(item_code=self.burger, restaurant=self.restaurant)
        admin = api.get_menu_admin(restaurant=self.restaurant)
        self.assertIn(self.burger, [i["code"] for i in admin["off_menu"]])


class TestEditingAnItem(MenuTestCase):
    def test_it_moves_between_categories(self):
        group = fx.item_group(self, "MENU-TEST-GROUP")
        api.update_menu_item(item_code=self.burger, group=group,
                             restaurant=self.restaurant)
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        self.assertEqual(item["group"], group)

    def test_a_category_that_does_not_exist_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.update_menu_item(item_code=self.burger, group="Not A Category",
                                 restaurant=self.restaurant)

    def test_a_kitchen_section_that_does_not_exist_is_refused(self):
        """A bad link here sends the line to no printer at all, which the
        kitchen notices as an order that never arrived."""
        with self.assertRaises(frappe.ValidationError):
            api.update_menu_item(item_code=self.burger, kitchen_group="nonsense",
                                 restaurant=self.restaurant)

    def test_tags_are_tidied_rather_than_stored_raw(self):
        api.update_menu_item(item_code=self.burger,
                             tags="Spicy, ,Vegan,Spicy,  Chef's Special ",
                             restaurant=self.restaurant)
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        self.assertEqual(item["tags"], "Spicy, Vegan, Chef's Special",
                         "a stray comma is a blank chip on the customer's menu")

    def test_an_absent_argument_changes_nothing(self):
        """The manager's two-field dialog and the owner's full editor call the
        same endpoint; neither may blank a field it does not show."""
        api.update_menu_item(item_code=self.burger, tags="Spicy",
                             restaurant=self.restaurant)
        api.update_menu_item(item_code=self.burger, rate=950,
                             restaurant=self.restaurant)
        item = next(i for i in M.menu_items(self.restaurant) if i["code"] == self.burger)
        self.assertEqual(item["tags"], "Spicy")
        self.assertEqual(item["rate"], 950)

    def test_an_out_of_range_guide_commission_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.update_menu_item(item_code=self.burger, guide_pct=80,
                                 restaurant=self.restaurant)

    def test_an_item_not_on_this_menu_cannot_be_edited_through_it(self):
        other = fx.item(self, "MENU-NOT-ON-IT")
        with self.assertRaises(frappe.ValidationError):
            api.update_menu_item(item_code=other, rate=100,
                                 restaurant=self.restaurant)


class TestAddingAnItem(MenuTestCase):
    def test_one_call_carries_the_whole_item(self):
        """Otherwise adding an item is "add it, then find it, then edit it"."""
        name = "MENU-NEW-ITEM"
        self.addCleanup(self._drop_item, name)
        api.add_menu_item(item_name=name, rate=450, group="Products",
                          tags="Vegan", prep_time=12, description="Test item",
                          restaurant=self.restaurant)
        item = next((i for i in M.menu_items(self.restaurant) if i["code"] == name), None)
        self.assertIsNotNone(item, "the new item should be on the menu")
        self.assertEqual(item["rate"], 450)
        self.assertEqual(item["tags"], "Vegan")
        self.assertEqual(item["prep_time"], 12)

    def _drop_item(self, name):
        frappe.db.delete("Restaurant Menu Item", {"parent": self.menu, "item": name})
        for dt, key in (("Item Price", "item_code"), ("Item", "name")):
            if frappe.db.exists(dt, {key: name}):
                try:
                    frappe.delete_doc(dt, frappe.db.get_value(dt, {key: name}, "name"),
                                      force=True, ignore_permissions=True)
                except Exception:
                    frappe.db.rollback()

    def test_a_price_of_zero_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.add_menu_item(item_name="MENU-FREE", rate=0, group="Products",
                              restaurant=self.restaurant)


class TestAddons(MenuTestCase):
    def setUp(self):
        super().setUp()
        if not frappe.db.exists("DocType", "POS Item Addon"):
            self.skipTest("POS Item Addon is not installed")
        self.cheese = fx.item(self, "MENU-CHEESE")
        fx.item_price(self, self.cheese, 100)
        self.on_menu(self.cheese, 100)
        self.addCleanup(frappe.db.delete, "POS Item Addon", {"item": self.burger})

    def test_the_set_is_replaced_not_merged(self):
        api.set_item_addons(item_code=self.burger, addons=[self.cheese])
        api.set_item_addons(item_code=self.burger, addons=[])
        items = M.menu_items(self.restaurant, with_addons=True)
        row = next(i for i in items if i["code"] == self.burger)
        self.assertEqual(row["addons"], [],
                         "a left-behind disabled row is a difference nobody can see")

    def test_the_till_is_offered_what_was_picked(self):
        api.set_item_addons(item_code=self.burger, addons=[self.cheese])
        items = M.menu_items(self.restaurant, with_addons=True)
        row = next(i for i in items if i["code"] == self.burger)
        self.assertEqual(row["addons"], [self.cheese])

    def test_an_item_cannot_be_its_own_addon(self):
        api.set_item_addons(item_code=self.burger, addons=[self.burger, self.cheese])
        items = M.menu_items(self.restaurant, with_addons=True)
        row = next(i for i in items if i["code"] == self.burger)
        self.assertEqual(row["addons"], [self.cheese])

    def test_an_addon_that_does_not_exist_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            api.set_item_addons(item_code=self.burger, addons=["NO-SUCH-ITEM"])


class TestThePhotoUpload(MenuTestCase):
    """Reachable by everyone who can edit the menu, so what it accepts matters:
    an upload path that takes anything is a file server."""

    def upload(self, filename, content):
        class _Upload:
            def __init__(self, name, body):
                self.filename = name
                self.stream = frappe._dict(read=lambda: body)

        previous = getattr(frappe.local, "request", None)
        frappe.local.request = frappe._dict(files={"file": _Upload(filename, content)})
        try:
            return api.upload_menu_image(item_code=self.burger)
        finally:
            frappe.local.request = previous

    def test_an_executable_pretending_to_be_a_photo_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.upload("payload.php", b"<?php ?>")

    def test_something_far_too_big_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self.upload("huge.jpg", b"x" * (M.MAX_IMAGE_BYTES + 1))

    def test_nothing_sent_is_refused(self):
        previous = getattr(frappe.local, "request", None)
        frappe.local.request = frappe._dict(files={})
        try:
            with self.assertRaises(frappe.ValidationError):
                api.upload_menu_image(item_code=self.burger)
        finally:
            frappe.local.request = previous


class TestWhoMayEditTheMenu(MenuTestCase):
    """The screen is the owner's; the endpoints are the manager's, so /manager
    can grow into them. Nobody below that gets to reprice the menu."""

    def as_role(self, role):
        email = f"lf-test-menu-{role.lower().replace(' ', '-')}@example.com"
        if not frappe.db.exists("User", email):
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "Menu Test",
                            "send_welcome_email": 0, "roles": [{"role": role}]}
                           ).insert(ignore_permissions=True)
        previous = frappe.session.user
        frappe.set_user(email)
        self.addCleanup(frappe.set_user, previous)

    def test_a_cashier_may_not_touch_it(self):
        self.as_role(roles.CASHIER)
        for endpoint, kwargs in (
                (api.get_menu_admin, {}),
                (api.set_menu_item_available, {"item_code": self.burger, "available": 0}),
                (api.remove_menu_item, {"item_code": self.burger}),
                (api.restore_menu_item, {"item_code": self.burger}),
                (api.set_item_addons, {"item_code": self.burger, "addons": []}),
                (api.upload_menu_image, {"item_code": self.burger})):
            with self.subTest(endpoint=endpoint.__name__):
                with self.assertRaises(frappe.PermissionError):
                    endpoint(**kwargs)

    def test_a_manager_may(self):
        self.as_role(roles.RESTAURANT_MANAGER)
        self.assertTrue(api.get_menu_admin()["restaurants"])

    def test_a_guest_may_not(self):
        previous = frappe.session.user
        frappe.set_user("Guest")
        self.addCleanup(frappe.set_user, previous)
        with self.assertRaises(frappe.PermissionError):
            api.get_menu_admin()
