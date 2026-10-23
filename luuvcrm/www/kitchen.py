import frappe

from luuvcrm import branch, roles

no_cache = 1


def get_context(context):
    """The kitchen display (/kitchen).

    Kitchen Staff and anyone above them: a waiter marks food served from here,
    and a cashier covering the pass needs it too. See roles.KITCHEN_ROLES.

    This used to set `redirect_location` for a guest and then fall through
    without raising, so the flag was set and ignored and the whole screen —
    every open order, table by table — rendered for anyone who asked. The raise
    is what makes a redirect a redirect.
    """
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/kitchen"
        raise frappe.Redirect

    roles.require_kitchen()

    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    user = frappe.session.user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    context.restaurant_name = branch.display_label()
    context.can_branch_requests = roles.has_any(roles.BRANCH_REQUEST_ROLES)
