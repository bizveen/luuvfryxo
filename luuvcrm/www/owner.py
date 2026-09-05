"""/owner — every branch side by side, and the setup behind them.

Owner-only. Phase 2c built the multi-restaurant machinery and left it reachable
only from a Python console; `roles.require_owner()` existed and nothing called
it. This is the screen that gives the role something to do.

A manager is deliberately refused here even though they can read their own
branch's numbers on /manager: this page is about *comparing* branches and
changing which one a till and a person belong to, and that is the owner's call.
"""

import frappe

from luuvcrm import branch, roles, settings

no_cache = 1
no_sitemap = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/owner"
        raise frappe.Redirect

    roles.require_owner()

    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    user = frappe.session.user
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    context.brand_name = settings.get("brand_name")
    context.branch_count = len(branch.all_restaurants())
    return context
