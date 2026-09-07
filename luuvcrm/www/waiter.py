
import frappe

from luuvcrm import branch, roles
from luuvcrm.api import menu_groups

no_cache = 1

def get_context(context):
    """The waiter PWA (/waiter). Waiter and anyone above — see roles.WAITER_ROLES;
    a cashier covering the floor can take orders too."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    if frappe.session.user == "Guest":
        import os
        login_path = os.path.join(os.path.dirname(__file__), "pos-login.html")
        if os.path.exists(login_path):
            with open(login_path, "r") as f:
                html = f.read()
            html = html.replace(
                'window.location.href = redirect || "/pos"',
                'window.location.href = redirect || "/waiter"'
            )
            html = html.replace("redirect=/pos", "redirect=/waiter")
            context["custom_login_html"] = html
            context["is_login"] = True
        return

    roles.require_waiter()

    user = frappe.session.user
    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()

    restaurant_name = branch.viewing()
    context.menu_groups = []
    context.tables = []
    context.service_charge_rate = 0

    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.service_charge_rate = float(restaurant.get("service_charge") or 0)
        context.menu_groups = menu_groups(restaurant_name)

    _tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    for t in _tables:
        parts = t.name.split("-")
        t["short_name"] = parts[-1] if len(parts) > 1 else t.name   # "Luuv-Fryxo-15" -> "15"
    context.tables = _tables
