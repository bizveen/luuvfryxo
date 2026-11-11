"""Manager 2.0 uses the same session, branch and role checks as Manager."""
import frappe
from luuvcrm.www.manager import get_context as manager_context

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/manager2.0"
        raise frappe.Redirect
    return manager_context(context)
