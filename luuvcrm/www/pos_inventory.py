"""Stock-aware counter, sharing the current POS screen and permissions."""
import frappe
from luuvcrm.www import pos

no_cache = 1


def get_context(context):
    context.inventory_pos = True
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/pos-inventory"
        raise frappe.Redirect
    pos.get_context(context)
