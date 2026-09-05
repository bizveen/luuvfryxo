import frappe

from luuvcrm import settings

from luuvcrm import roles

no_cache = 1
no_sitemap = 1


def get_context(context):
    """QR Promo sticker admin (/qrpromo). Manager-only — it creates sticker
    batches, sets which item is given away free, and reads the register/redeem
    stats. "Staff-only" was the intent; a login was the only thing enforcing it,
    so any signed-in user could mint promo codes."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/qrpromo"
        raise frappe.Redirect

    roles.require_manager()

    context.csrf_token = frappe.sessions.get_csrf_token()
    context.base_url = frappe.utils.get_url()
    context.wa_number = settings.get("wa_business_number")
    return context
