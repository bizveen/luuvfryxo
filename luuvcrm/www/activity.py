import frappe

from luuvcrm import branch, roles

no_cache = 1
no_sitemap = 1


def get_context(context):
    """Staff activity trail (/activity). Manager-only — it shows every cashier's
    taps, bill totals, drawer movements and shift variances."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/activity"
        raise frappe.Redirect

    # `MANAGER_ROLES` used to be imported from luuvcrm.activity, which stopped
    # defining it when the role sets were consolidated into luuvcrm.roles — so
    # this module raised ImportError before get_context was ever called and the
    # page was a 500 for everyone, manager included. Gate through roles.py.
    roles.require_manager()
    user = frappe.session.user

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.current_user = user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()
    # Deep link from /manager bill detail: /activity?bill=00941
    context.preset_bill = (frappe.form_dict.get("bill") or "").strip()
    context.preset_order = (frappe.form_dict.get("order") or "").strip()
    restaurants = branch.available_restaurants(user)
    if not restaurants:
        all_restaurants = branch.all_restaurants()
        restaurants = all_restaurants if len(all_restaurants) == 1 else []
    context.restaurants = [frappe._dict(name=name, label=branch.display_label(name))
                           for name in restaurants]
    context.active_restaurant = branch.viewing(user) or ""
    return context
