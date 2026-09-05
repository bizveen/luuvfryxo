from itertools import groupby

import frappe

from luuvcrm import branch
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    restaurant_name = branch.current()
    context.restaurant = None
    context.menu_groups = []
    context.opening_time = "11:00 AM"
    context.closing_time = "11:00 PM"

    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.restaurant = restaurant
        active_menu = restaurant.active_menu

        if active_menu:
            menu = frappe.get_doc("Restaurant Menu", active_menu)
            if menu.enabled:
                items = []
                for row in menu.items:
                    if not row.item:
                        continue
                    doc = frappe.get_doc("Item", row.item)
                    items.append({
                        "code": row.item,
                        "name": doc.item_name or row.item,
                        "image": doc.image or "",
                        "rate": _resolve_item_rate(row.item, row.rate, _get_pos_price_list()),
                        "group": doc.item_group or "General",
                    })
                items.sort(key=lambda x: (x["group"], x["rate"]))
                groups = []
                for key, grp in groupby(items, key=lambda x: x["group"]):
                    groups.append({"name": key, "items": list(grp)})
                context.menu_groups = groups
