import frappe
from frappe import _

no_cache = 1

MANAGER_ROLES = {"System Manager", "Administrator", "Restaurant Manager"}


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/manager"
        raise frappe.Redirect

    # The session check above only proved the visitor is logged in — that let any
    # waiter or website customer open the manager app. The endpoints behind it now
    # call _require_manager(), so gate the page on the same roles rather than
    # rendering a dashboard that 403s on every panel.
    if not (set(frappe.get_roles(frappe.session.user)) & MANAGER_ROLES):
        frappe.throw(_("Only a manager can open this page"), frappe.PermissionError)

    context.csrf_token = frappe.sessions.get_csrf_token()
    user = frappe.session.user
    full_name = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    context.current_user = user
    context.current_user_full = full_name

    # The old soft PIN lock ("4321") was rendered into client JavaScript, so it was
    # readable in view-source — and its gate had already been disabled in the page.
    # Removed rather than left as a dead constant that reads like protection.

    return context
