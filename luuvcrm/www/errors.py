import frappe

from luuvcrm import roles

no_cache = 1
no_sitemap = 1


def get_context(context):
    """Front-end error log (/errors). Manager-only — it exposes stack traces, page
    URLs and device strings from every staff phone."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/errors"
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
    return context
