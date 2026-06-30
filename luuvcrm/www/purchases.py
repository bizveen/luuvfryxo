import frappe

no_cache = 1


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/purchases"
        raise frappe.Redirect

    context.csrf_token = frappe.sessions.get_csrf_token()
    user = frappe.session.user
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    return context
