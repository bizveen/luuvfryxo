import frappe
from itertools import groupby
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1
login_required = False


def redirect_to_cus():
    """301 to /cus, carrying the query string.

    Three old routes still reach customers: /customer (the PWA's address until
    2026-07-12), /myorders and /my_orders. They are links already sent — in
    WhatsApp threads, on printed QR stickers — so they are kept as aliases
    rather than deleted, and each is four lines of redirect over this.

    The query string has to survive: ?phone= comes from a WhatsApp link, ?ref=
    from a referral, ?promo= from a sticker QR, and dropping it turns a
    personalised link into a generic one.
    """
    qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
    frappe.local.flags.redirect_location = "/cus" + (("?" + qs) if qs else "")
    raise frappe.Redirect


def get_context(context):
    """Customer PWA (/cus — moved off /app, which belongs to Frappe Desk;
    previously /customer, shortened to /cus for WhatsApp/QR links).
    Guest-allowed — auth is the phone + 4-digit PIN token scheme
    (customer_* / loyalty_* APIs), not the Frappe session.
    Mirrors order.py for the menu and manager.py for the PWA shell."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    # Deep-link params: ?ref=<referral_code> (referral), ?phone=<mobile> (WhatsApp link),
    # ?promo=<sticker QR code> (free-meal campaign)
    context.ref_code = (frappe.form_dict.get("ref", "") or "").strip().upper()
    context.prefill_phone = (frappe.form_dict.get("phone", "") or "").strip()
    context.promo_code = (frappe.form_dict.get("promo", "") or "").strip().upper()

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
                        "rate": _resolve_item_rate(row.item, row.rate, _get_pos_price_list()),
                        "group": doc.item_group or "General",
                    })
                items.sort(key=lambda x: (x["group"], x["rate"]))
                groups = []
                for key, grp in groupby(items, key=lambda x: x["group"]):
                    groups.append({"name": key, "items": list(grp)})
                context.menu_groups = groups
