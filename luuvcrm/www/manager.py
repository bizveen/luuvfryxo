import frappe
from frappe import _

no_cache = 1


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/manager"
        raise frappe.Redirect

    context.csrf_token = frappe.sessions.get_csrf_token()
    user = frappe.session.user
    full_name = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    context.current_user = user
    context.current_user_full = full_name

    # Soft PIN lock on the shared phone (real security is the Frappe session above).
    # Configurable later; default keeps the design's gate.
    context.manager_pin = "4321"

    return context
