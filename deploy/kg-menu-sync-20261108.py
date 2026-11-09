"""Sync the Kurunegala customer menu from the supplied full-menu artwork.

Run from ``bench --site test.localhost console`` with:
    %run /home/pc/frappe-bench/apps/luuvcrm/deploy/kg-menu-sync-20261108.py

The image files are exact crops from the artwork and must already be present in
the site's public/files directory.  Dambulla's menu is deliberately untouched.
"""

import json
import os

import frappe
from frappe.utils import now

from luuvcrm.api.menu import _set_pos_item_price, menu_items
from luuvcrm.api.pricing import _get_pos_price_list


RESTAURANT = "Kurunegala"
IMAGE_STAMP = "20261028"


def item(code, name, group, rate, description):
    return {
        "code": "KG-" + code,
        "name": name,
        "group": group,
        "rate": rate,
        "description": description,
        "image": "/files/kg-%s-%s.jpg" % (code.lower(), IMAGE_STAMP),
    }


ITEMS = [
    item(
        "FRIES-CHICKEN",
        "Crispy Chicken Loaded Fries",
        "Loaded Fries",
        1890,
        "Crispy chicken, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.",
    ),
    item(
        "FRIES-FISH",
        "Crispy Fish Loaded Fries",
        "Loaded Fries",
        2290,
        "Crispy fish, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, White Sauce, Golden Sauce and Smoky Brown Sauce.",
    ),
    item(
        "FRIES-PRAWN",
        "Tempura Prawns Loaded Fries",
        "Loaded Fries",
        2590,
        "Tempura prawns, creamy coleslaw, mozzarella cheese, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.",
    ),
    item(
        "FRIES-BEEF",
        "Ground Beef Spicy Masala Loaded Fries",
        "Loaded Fries",
        2290,
        "Spicy masala ground beef, garden mayo salad, mozzarella cheese, Luuv Chef special Hot Sauce, Smoky Brown Sauce, White Sauce and Golden Sauce.",
    ),
    item(
        "FRIES-SAUSAGE",
        "Grilled Bratwurst Sausage Loaded Fries",
        "Loaded Fries",
        1690,
        "Grilled bratwurst sausage, creamy salad, mozzarella cheese, Smoky Brown Sauce, Golden Sauce, White Sauce and Luuv Chef special Hot Sauce.",
    ),
    item(
        "FRIES-MIX",
        "Mix Meat Loaded Fries",
        "Loaded Fries",
        3590,
        "Crispy chicken, prawns and sausage with golden fries, creamy coleslaw, mozzarella cheese and signature sauces.",
    ),
    item(
        "CHIPS-FISH",
        "Fish and Chips",
        "Chips",
        990,
        "Crispy fish with crunchy potato chips, mixed garden creamy salad, mozzarella cheese, Luuv Chef special Hot Sauce and Smoky Brown Sauce.",
    ),
    item(
        "CHIPS-WIENILET",
        "Crispy Wings and Chips",
        "Chips",
        790,
        "Crispy chicken wings with crunchy potato chips, mixed garden creamy salad, mozzarella cheese, Luuv Chef special Hot Sauce and Smoky Brown Sauce.",
    ),
    item(
        "CHIPS-SPICY",
        "Spicy Chips",
        "Chips",
        490,
        "Crispy potato chips tossed with spicy seasoning, served with creamy salad, Smoky Brown Sauce and Luuv Chef special Hot Sauce.",
    ),
    item("THAI-BLUEBERRY", "Thai Milk Iced Tea with Blueberry (700ml)", "Drinks", 990, "Thai milk iced tea with blueberry, served in a 700ml cup."),
    item("THAI-STRAWBERRY", "Thai Tea with Strawberry (700ml)", "Drinks", 790, "Thai tea with strawberry, served in a 700ml cup."),
    item("THAI-MANGO", "Thai Tea with Mango (700ml)", "Drinks", 790, "Thai tea with mango, served in a 700ml cup."),
    item("SMOOTHIE-MANGO", "Mango Smoothie (700ml)", "Drinks", 890, "Mango smoothie, served in a 700ml cup."),
    item("SMOOTHIE-BLUEBERRY", "Blueberry Smoothie (700ml)", "Drinks", 1090, "Blueberry smoothie, served in a 700ml cup."),
    item("SMOOTHIE-PASSION", "Passion Smoothie (700ml)", "Drinks", 890, "Passion fruit smoothie, served in a 700ml cup."),
    item("SMOOTHIE-STRAWBERRY", "Strawberry Smoothie (700ml)", "Drinks", 890, "Strawberry smoothie, served in a 700ml cup."),
    item("ICED-COFFEE", "Iced Coffee (700ml)", "Drinks", 990, "Iced coffee, served in a 700ml cup."),
    item("ICED-CHOCOLATE", "Iced Chocolate (700ml)", "Drinks", 990, "Iced chocolate, served in a 700ml cup."),
    item("LEMONADE-BLUEBERRY", "Blueberry Lemonade (300ml)", "Drinks", 590, "Blueberry lemonade, served in a 300ml cup."),
    item("LEMONADE-STRAWBERRY", "Strawberry Lemonade (300ml)", "Drinks", 590, "Strawberry lemonade, served in a 300ml cup."),
    item("LEMONADE-PASSION", "Passion Lemonade (300ml)", "Drinks", 490, "Passion fruit lemonade, served in a 300ml cup."),
    item("LEMON-MINT", "Lemon and Mint (300ml)", "Drinks", 490, "Lemon and mint drink, served in a 300ml cup."),
    item("EXTRA-BROWN", "Smoky Brown Sauce", "Add-ons", 100, "Extra Smoky Brown Sauce."),
    item("EXTRA-WHITE", "White Sauce", "Add-ons", 100, "Extra White Sauce."),
    item("EXTRA-GOLDEN", "Golden Sauce", "Add-ons", 100, "Extra Golden Sauce."),
    item("EXTRA-SALSA", "Tomato Salsa", "Add-ons", 100, "Extra Tomato Salsa."),
    item("EXTRA-HOT", "Luuv Chef Special Hot Sauce", "Add-ons", 100, "Extra Luuv Chef Special Hot Sauce."),
    item("EXTRA-CHEESE", "Cheese Slices", "Add-ons", 200, "Extra cheese slices."),
]


restaurant = frappe.get_doc("Restaurant", RESTAURANT)
menu_name = restaurant.active_menu
assert menu_name, "Kurunegala has no active menu"
menu = frappe.get_doc("Restaurant Menu", menu_name)
assert menu.restaurant == RESTAURANT, "Active menu belongs to another restaurant"
assert len(ITEMS) == 28
assert len({row["code"] for row in ITEMS}) == len(ITEMS)

dambulla_before = json.dumps(menu_items("Dambulla"), sort_keys=True, default=str)
for row in ITEMS:
    image_path = frappe.get_site_path("public", row["image"].lstrip("/"))
    assert os.path.exists(image_path), image_path

for group in ("Loaded Fries", "Chips", "Drinks", "Add-ons"):
    if not frappe.db.exists("Item Group", group):
        frappe.get_doc({
            "doctype": "Item Group",
            "item_group_name": group,
            "parent_item_group": "All Item Groups",
            "is_group": 0,
        }).insert(ignore_permissions=True)

kitchen_groups = {}
if frappe.db.exists("DocType", "Kitchen Group"):
    kitchen_groups = {
        row.group_name: row.name
        for row in frappe.get_all("Kitchen Group", fields=["name", "group_name"], limit_page_length=0)
    }

price_list = _get_pos_price_list()
for row in ITEMS:
    values = {
        "item_name": row["name"],
        "item_group": row["group"],
        "description": row["description"],
        "image": row["image"],
        "standard_rate": row["rate"],
        "disabled": 0,
        "is_sales_item": 1,
    }
    if frappe.db.has_column("Item", "kitchen_group"):
        values["kitchen_group"] = kitchen_groups.get(
            "Beverages" if row["group"] == "Drinks" else "Sides & Starters", ""
        )

    if frappe.db.exists("Item", row["code"]):
        frappe.db.set_value("Item", row["code"], values)
    else:
        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": row["code"],
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "is_purchase_item": 0,
            **values,
            "item_defaults": [{
                "company": restaurant.company,
                "default_warehouse": restaurant.warehouse,
                "selling_cost_center": restaurant.cost_center,
            }],
        })
        doc.insert(ignore_permissions=True)
    _set_pos_item_price(row["code"], row["rate"], price_list)

# Replace only this branch's active-menu rows.  The Items and historical sales
# are retained; the three demo rows simply stop appearing on Kurunegala's menu.
frappe.db.delete("Restaurant Menu Item", {"parent": menu_name, "parenttype": "Restaurant Menu"})
has_available = frappe.db.has_column("Restaurant Menu Item", "available")
for idx, row in enumerate(ITEMS, 1):
    child = {
        "doctype": "Restaurant Menu Item",
        "parent": menu_name,
        "parenttype": "Restaurant Menu",
        "parentfield": "items",
        "idx": idx,
        "item": row["code"],
        "rate": row["rate"],
    }
    if has_available:
        child["available"] = 1
    frappe.get_doc(child).insert(ignore_permissions=True)

frappe.db.set_value("Restaurant Menu", menu_name, "modified", now(), update_modified=False)
frappe.clear_cache(doctype="Item")
frappe.clear_cache(doctype="Restaurant Menu")

actual = menu_items(RESTAURANT)
assert [row["code"] for row in actual] == [row["code"] for row in ITEMS]
assert {row["code"]: row["rate"] for row in actual} == {
    row["code"]: row["rate"] for row in ITEMS
}
assert all(row["image"] for row in actual)
assert json.dumps(menu_items("Dambulla"), sort_keys=True, default=str) == dambulla_before

frappe.db.commit()
frappe.clear_cache()
try:
    from frappe.website.utils import clear_website_cache

    clear_website_cache()
except ImportError:
    pass

print(json.dumps({
    "ok": True,
    "restaurant": RESTAURANT,
    "menu": menu_name,
    "items": len(actual),
    "price_list": price_list,
    "categories": {
        group: sum(1 for row in actual if row["group"] == group)
        for group in ("Loaded Fries", "Chips", "Drinks", "Add-ons")
    },
    "lemonade_prices": {
        row["name"]: row["rate"] for row in actual if "Lemon" in row["name"]
    },
    "dambulla_unchanged": True,
}, indent=2))
