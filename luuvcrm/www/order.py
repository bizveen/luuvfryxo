
import frappe

from luuvcrm import branch, settings
from luuvcrm.api import menu_groups

no_cache = 1
login_required = False

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()
    table_name = frappe.form_dict.get("table", "")
    context.selected_table = table_name

    # The table is the strongest thing this request carries: a QR sticker on
    # table 5 says which branch as well as which table.
    restaurant_name = branch.for_page(context)
    context.restaurant_name = restaurant_name or settings.get("brand_name")
    context.menu_groups = []
    context.tables = []

    if not restaurant_name:
        return

    context.menu_groups = menu_groups(restaurant_name)
    # This branch's tables only. Listing every table let a customer in one
    # branch put an order on a table standing in the other one.
    context.tables = frappe.get_all("Restaurant Table", filters={"restaurant": restaurant_name},
                                    fields=["name"], order_by="name asc")
