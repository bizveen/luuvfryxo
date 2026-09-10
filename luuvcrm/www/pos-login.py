import frappe

from luuvcrm import settings

no_cache = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    # /staff forwards floor staff to their own screen and shows managers the
    # apps to choose from. /pos was right for a cashier and a 403 for a waiter.
    redirect = frappe.local.form_dict.get("redirect", "/staff")
    context.redirect = redirect
    context.title = f"Login - {settings.get('brand_name')}"
