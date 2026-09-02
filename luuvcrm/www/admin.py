import frappe
from frappe import _

no_cache = 1
no_sitemap = 1


def get_context(context):
    """Admin hub (/admin). Manager-only launcher for every Luuv Fryxo sub-app,
    with a slim live-status strip (full dashboards live in /manager)."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/admin"
        raise frappe.Redirect

    user = frappe.session.user
    if "System Manager" not in frappe.get_roles(user):
        frappe.throw(_("Only managers can access the admin hub"), frappe.PermissionError)

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    return context
