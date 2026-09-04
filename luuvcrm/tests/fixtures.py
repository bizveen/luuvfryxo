"""Test data builders.

Tests build the rows they need rather than leaning on whatever the site happens
to contain, so a run means the same thing on a developer's laptop, on CI and on
a fresh site. Every builder registers its cleanup with the calling test.

Names are prefixed `_LFT-` so a failed run leaves something obvious behind
rather than something that looks like real data.
"""

import frappe
from frappe.utils import add_days, today

PREFIX = "_LFT-"


def _cleanup(case, doctype, name):
    def drop():
        if frappe.db.exists(doctype, name):
            frappe.delete_doc(doctype, name, force=True, ignore_permissions=True,
                              delete_permanently=True)
    case.addCleanup(drop)


def item_group(case, name=PREFIX + "Group"):
    if not frappe.db.exists("Item Group", name):
        parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name")
        frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
                        "parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)
        _cleanup(case, "Item Group", name)
    return name


def item(case, code, standard_rate=0):
    """An item with a standard_rate but deliberately NO Item Price.

    standard_rate is written after the insert, on purpose. ERPNext's Item
    controller creates an Item Price from standard_rate on validate, so passing
    it to insert() would give every test item a hidden Standard Selling price —
    which sits at the top of the resolver's priority list and makes the rest of
    the fallback chain untestable.
    """
    code = PREFIX + code
    if frappe.db.exists("Item", code):
        frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": item_group(case),
        "stock_uom": "Nos",
        "is_stock_item": 0,
    }).insert(ignore_permissions=True)
    _cleanup(case, "Item", code)
    if standard_rate:
        frappe.db.set_value("Item", code, "standard_rate", standard_rate)
    for name in frappe.get_all("Item Price", filters={"item_code": code}, pluck="name"):
        frappe.delete_doc("Item Price", name, force=True, ignore_permissions=True)
    return code


def item_price(case, item_code, rate, price_list="Standard Selling",
               valid_from=None, valid_upto=None, customer=None, selling=1):
    doc = frappe.get_doc({
        "doctype": "Item Price",
        "item_code": item_code,
        "price_list": price_list,
        "price_list_rate": rate,
        "selling": selling,
        "buying": 0 if selling else 1,
        "valid_from": valid_from,
        "valid_upto": valid_upto,
        "customer": customer,
    }).insert(ignore_permissions=True)
    _cleanup(case, "Item Price", doc.name)
    return doc.name


def price_list(case, name):
    name = PREFIX + name
    if not frappe.db.exists("Price List", name):
        frappe.get_doc({"doctype": "Price List", "price_list_name": name,
                        "selling": 1, "enabled": 1,
                        "currency": frappe.db.get_default("currency") or "LKR"
                        }).insert(ignore_permissions=True)
        _cleanup(case, "Price List", name)
    return name


def menu_rate(case, item_code, rate):
    """Put an item on the active Restaurant Menu at `rate`, and drop the row after.

    This is the fallback that made a client-supplied rate necessary: an item can
    be priced here and nowhere else.

    The child row is inserted directly rather than through menu.save(), because
    hospitality's Restaurant Menu controller rebuilds the menu's whole price list
    on every save — `clear_item_price` runs a bare
    `delete from tabItem Price where price_list = %s` — which would wipe prices
    the test (and anything else on the site) depends on.
    """
    restaurant = frappe.get_value("Restaurant", {}, "name")
    menu_name = frappe.db.get_value("Restaurant", restaurant, "active_menu")
    if not menu_name:
        raise RuntimeError("the test site has no active Restaurant Menu")
    row = frappe.get_doc({
        "doctype": "Restaurant Menu Item",
        "parent": menu_name,
        "parenttype": "Restaurant Menu",
        "parentfield": "items",
        "item": item_code,
        "rate": rate,
        "idx": 9000,
    })
    row.db_insert()

    def drop():
        frappe.db.delete("Restaurant Menu Item", {"name": row.name})
        forget_menu_cache()

    case.addCleanup(drop)
    forget_menu_cache()
    return row.name


def forget_menu_cache():
    """_menu_row_rates() caches for the life of a request; a test changes the
    menu inside one."""
    frappe.local._lf_menu_rates = None


def service_charge(case, percent):
    """Set the restaurant's service charge for one test and put it back after."""
    restaurant = frappe.get_value("Restaurant", {}, "name")
    previous = frappe.db.get_value("Restaurant", restaurant, "service_charge")
    frappe.db.set_value("Restaurant", restaurant, "service_charge", percent)
    case.addCleanup(frappe.db.set_value, "Restaurant", restaurant, "service_charge", previous)
    return restaurant


def days_ago(n):
    return add_days(today(), -n)


def yesterday():
    return days_ago(1)


def tomorrow():
    return add_days(today(), 1)
