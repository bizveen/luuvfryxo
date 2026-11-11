"""Purchase 2.0 reuses the existing purchasing access and branch context."""
import frappe
from luuvcrm.www.purchases import get_context as purchases_context

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/purchase2.0"
        raise frappe.Redirect
    return purchases_context(context)
