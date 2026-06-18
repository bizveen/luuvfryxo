import frappe
from frappe import _
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    if not restaurant_name:
        context.restaurant = None
        context.menu_items = []
        return

    restaurant = frappe.get_doc("Restaurant", restaurant_name)
    active_menu_name = restaurant.active_menu

    context.restaurant = restaurant
    context.menu_items = []

    if not active_menu_name:
        return

    menu = frappe.get_doc("Restaurant Menu", active_menu_name)
    if not menu.enabled:
        return

    price_list = _get_pos_price_list()

    for item_row in menu.items:
        item_code = item_row.item
        if not item_code:
            continue

        item_doc = frappe.get_doc("Item", item_code)
        context.menu_items.append({
            "code": item_code,
            "name": item_doc.item_name or item_code,
            "description": item_doc.description or "",
            "image": item_doc.image or "",
            "rate": _resolve_item_rate(item_code, item_row.rate, price_list),
            "group": item_doc.item_group or "General",
        })

    context.menu_items.sort(key=lambda x: x.get("rate", 0))
