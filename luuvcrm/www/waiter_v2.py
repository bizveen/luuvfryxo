"""Delivery-only floor app, sharing the existing waiter session and branch."""
import frappe
from luuvcrm import branch,roles
no_cache=1
def get_context(context):
    if frappe.session.user=='Guest':
        frappe.local.flags.redirect_location='/pos-login?redirect=/waiter2.0'
        raise frappe.Redirect
    roles.require_waiter()
    context.no_sidebar=True;context.no_header=True;context.no_footer=True
    context.csrf_token=frappe.sessions.get_csrf_token()
    context.current_user_full=frappe.get_value('User',frappe.session.user,'full_name') or frappe.session.user
    context.restaurant_name=branch.display_label(branch.viewing())
    context.today=frappe.utils.today()
