
import frappe

from luuvcrm import branch, settings
from luuvcrm.api import menu_groups

no_cache = 1
no_sitemap = 1
login_required = False

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    table_name = frappe.form_dict.get("table", "")
    context.selected_table = table_name

    restaurant_name = branch.viewing()
    context.restaurant_name = restaurant_name or settings.get("brand_name")
    context.menu_groups = []
    context.tables = []

    if restaurant_name:
        context.menu_groups = menu_groups(restaurant_name)

    context.tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
