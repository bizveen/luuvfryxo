
import frappe

from luuvcrm import branch, settings
from luuvcrm.api import menu_groups

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

    restaurant_name = branch.viewing()
    context.restaurant_name = restaurant_name or settings.get("brand_name")
    context.menu_groups = []

    # Build the menu (same logic as the online order page).
    if restaurant_name:
        context.menu_groups = menu_groups(restaurant_name)
