import frappe
from frappe import _

from luuvcrm.activity import MANAGER_ROLES

no_cache = 1
no_sitemap = 1


def get_context(context):
    """Staff activity trail (/activity). Manager-only — it shows every cashier's
    taps, bill totals, drawer movements and shift variances."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/activity"
        raise frappe.Redirect

    user = frappe.session.user
    if not (set(frappe.get_roles(user)) & MANAGER_ROLES):
        frappe.throw(_("Only managers can access the activity log"), frappe.PermissionError)

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    # Deep link from /manager bill detail: /activity?bill=00941
    context.preset_bill = (frappe.form_dict.get("bill") or "").strip()
    context.preset_order = (frappe.form_dict.get("order") or "").strip()
    return context
