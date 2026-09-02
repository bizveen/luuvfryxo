import frappe

no_cache = 1
no_sitemap = 1


def get_context(context):
    """QR Promo sticker admin (/qrpromo). Staff-only — create sticker batches,
    print QR labels, set the free-meal item, track register/redeem stats."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/qrpromo"
        raise frappe.Redirect

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.base_url = frappe.utils.get_url()
    from luuvcrm.api import PROMO_WA_NUMBER
    context.wa_number = PROMO_WA_NUMBER
    return context
