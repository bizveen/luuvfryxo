"""Original public landing, kept separate from the new /home design."""
from urllib.parse import urlencode

import frappe

from luuvcrm import branch
from luuvcrm.api import menu_groups

no_cache = 1


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    restaurant_name = branch.for_page(context)
    # This is the original Dambulla landing. A fresh visitor should see that
    # page as before; an explicit/table/remembered branch still takes priority.
    if not restaurant_name and frappe.db.exists("Restaurant", "Luuv Fryxo"):
        restaurant_name = "Luuv Fryxo"
        context.branch = restaurant_name
        context.branch_choices = []
        branch.remember(restaurant_name)

    context.restaurant = None
    context.menu_groups = []
    context.opening_time = "11:00 AM"
    context.closing_time = "11:00 PM"
    query = "?" + urlencode({"r": restaurant_name}) if restaurant_name else ""
    context.menu_url = "/menu" + query
    context.order_url = "/order" + query
    context.restaurant_label = ""
    context.location_label = ""
    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.restaurant = restaurant
        context.restaurant_label = restaurant.get("restaurant_name") or restaurant.name
        context.location_label = restaurant.get("city") or restaurant.name
        context.menu_groups = menu_groups(restaurant_name)
