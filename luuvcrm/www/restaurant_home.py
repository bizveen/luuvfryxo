
import frappe

from luuvcrm import branch
from luuvcrm.api import menu_groups

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
        context.menu_groups = menu_groups(restaurant_name)
