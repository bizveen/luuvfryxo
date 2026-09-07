"""/admin — the whole back office in one page.

This was a launcher: tiles that opened seven other screens. Everything an owner
had to *change* — a branch, a person, a price, an item — was either on /owner or
in the Frappe Desk. /owner is folded in here now, and the Desk is no longer the
only way to run the restaurant.

The gate is manager-level, as it was. The two sections that change the shape of
the business — Restaurants and Staff — are the owner's, and the page is told
which it is so it can leave those out rather than show a control the endpoint
will refuse. That is a courtesy, not the security: every endpoint behind them
calls `roles.require_owner()` itself.
"""

import frappe

from luuvcrm import roles

no_cache = 1
no_sitemap = 1


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/admin"
        raise frappe.Redirect

    # This said System Manager, and System Manager is deliberately not a business
    # role — the restaurant owner is not supposed to hold it (roles.py). So the
    # people the screen is for could not open it, while the endpoints behind it
    # already gate on roles.require_manager(). The page now agrees with them.
    roles.require_manager()
    user = frappe.session.user

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    context.is_owner = roles.is_owner()
    return context
