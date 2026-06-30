import frappe
from frappe import _
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1

def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        # Send guests to the dedicated POS login screen (/pos-login), not an inline form.
        frappe.local.flags.redirect_location = "/pos-login?redirect=/pos2"
        raise frappe.Redirect

    context.csrf_token = frappe.sessions.get_csrf_token()

    user = frappe.session.user
    full_name = frappe.get_value('User', user, 'full_name') or user.split('@')[0].title()
    context.current_user = user
    context.current_user_full = full_name

    allowed_roles = {'System Manager', 'POS User', 'Sales User', 'Cashier', 'Administrator'}
    user_roles = set(frappe.get_roles(user))
    if not (user_roles & allowed_roles) and not frappe.has_permission('Sales Invoice', 'create'):
        frappe.throw('You do not have permission to access POS', frappe.PermissionError)

    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    context.restaurant = None
    context.menu_groups = []
    context.items_flat = []
    context.tables = []
    context.service_charge_rate = 0

    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.restaurant = restaurant
        context.service_charge_rate = float(restaurant.get("service_charge") or 0)
        active_menu = restaurant.active_menu
        price_list = _get_pos_price_list()

        if active_menu:
            menu = frappe.get_doc("Restaurant Menu", active_menu)
            if menu.enabled:
                items = []
                for item_row in menu.items:
                    item_code = item_row.item
                    if not item_code:
                        continue
                    item_doc = frappe.get_doc("Item", item_code)
                    entry = {
                        "code": item_code,
                        "name": item_doc.item_name or item_code,
                        "description": item_doc.description or "",
                        "image": item_doc.image or "",
                        "rate": _resolve_item_rate(item_code, item_row.rate, price_list),
                        "group": item_doc.item_group or "General",
                    }
                    items.append(entry)

                items.sort(key=lambda x: (x.get("group", ""), x.get("rate", 0)))
                context.items_flat = items

                from itertools import groupby
                groups = []
                for key, group in groupby(items, key=lambda x: x["group"]):
                    groups.append({"name": key, "items": list(group)})
                context.menu_groups = groups

    _tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    for t in _tables:
        parts = t.name.split("-")
        t["short_name"] = parts[-1] if len(parts) > 1 else t.name
    context.tables = _tables

    opening = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": user},
        ["name", "period_start_date"], as_dict=True)
    context.shift = {"status": "open"} if opening else None

    ongoing_count = frappe.get_all("POS Order",
        filters={"docstatus": 0, "order_source": "Walk-in", "pos_invoice": ""},
        pluck="name"
    )
    context.ongoing_count = len(ongoing_count)

    context.pos_profiles = frappe.get_all("POS Profile", fields=["name", "warehouse", "company", "currency"])
    context.payment_modes = frappe.get_all("Mode of Payment", fields=["name"], order_by="name asc")

    # Payment methods per POS Profile (for the native opening screen)
    import json as _json
    _pp = {}
    for _p in context.pos_profiles:
        _modes = frappe.get_all("POS Payment Method",
            filters={"parent": _p.name}, fields=["mode_of_payment"], order_by="idx")
        _pp[_p.name] = [m.mode_of_payment for m in _modes if m.mode_of_payment] or ["Cash"]
    context.pos_profile_payments_json = _json.dumps(_pp)
