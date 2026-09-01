import frappe
from frappe import _
from itertools import groupby
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1

def get_context(context):
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

    user = frappe.session.user

    # Role gate: mirror /pos (www/pos.py). Only floor/POS staff may open the waiter
    # app. Blocks customer accounts (created via /cus) that would otherwise reach it,
    # since the waiter APIs bypass DocType permissions and enforce nothing themselves.
    allowed_roles = {'System Manager', 'Administrator', 'POS User', 'Sales User',
                     'Cashier', 'Waiter', 'Restaurant Manager'}
    if not (set(frappe.get_roles(user)) & allowed_roles):
        frappe.throw(_("You do not have permission to access the Waiter app"),
                     frappe.PermissionError)

    context.current_user_full = frappe.get_value("User", user, "full_name") or user.split("@")[0].title()

    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    context.menu_groups = []
    context.tables = []
    context.service_charge_rate = 0

    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.service_charge_rate = float(restaurant.get("service_charge") or 0)
        if restaurant.active_menu:
            menu = frappe.get_doc("Restaurant Menu", restaurant.active_menu)
            if menu.enabled:
                items = []
                for row in menu.items:
                    if not row.item:
                        continue
                    doc = frappe.get_doc("Item", row.item)
                    items.append({
                        "code": row.item,
                        "name": doc.item_name or row.item,
                        "image": doc.image or "",
                        "rate": _resolve_item_rate(row.item, row.rate, _get_pos_price_list()),
                        "group": doc.item_group or "General",
                    })
                items.sort(key=lambda x: (x["group"], x["rate"]))
                groups = []
                for key, grp in groupby(items, key=lambda x: x["group"]):
                    groups.append({"name": key, "items": list(grp)})
                context.menu_groups = groups

    _tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    for t in _tables:
        parts = t.name.split("-")
        t["short_name"] = parts[-1] if len(parts) > 1 else t.name   # "Luuv-Fryxo-15" -> "15"
    context.tables = _tables
