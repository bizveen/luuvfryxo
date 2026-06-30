import frappe
from itertools import groupby

no_cache = 1
login_required = False


def get_context(context):
    """Customer PWA (/app). Guest-allowed — auth is the phone + 4-digit PIN
    token scheme (customer_* / loyalty_* APIs), not the Frappe session.
    Mirrors order.py for the menu and manager.py for the PWA shell."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    # Deep-link params: ?ref=<referral_code> (referral) and ?phone=<mobile> (WhatsApp link)
    context.ref_code = (frappe.form_dict.get("ref", "") or "").strip().upper()
    context.prefill_phone = (frappe.form_dict.get("phone", "") or "").strip()

    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    context.restaurant_name = restaurant_name or "Luuv Fryxo"
    context.menu_groups = []

    # Build the menu (same logic as the online order page).
    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
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
                        "rate": row.rate or 0,
                        "group": doc.item_group or "General",
                    })
                items.sort(key=lambda x: (x["group"], x["rate"]))
                groups = []
                for key, grp in groupby(items, key=lambda x: x["group"]):
                    groups.append({"name": key, "items": list(grp)})
                context.menu_groups = groups
