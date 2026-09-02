import frappe

no_cache = 1
no_sitemap = 1


def get_context(context):
    """Free-meal claim form (/claim?phone=..&code=..). Linked from the WhatsApp
    auto-reply after a sticker QR claim — the phone is already proven, so the
    form only collects name + PIN and signs the customer straight into /app."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()

    from luuvcrm.api import _valid_claim_pair
    phone = (frappe.form_dict.get("phone", "") or "").strip()
    code = (frappe.form_dict.get("code", "") or "").strip().upper()

    pair = _valid_claim_pair(phone, code)
    context.valid = bool(pair)
    context.phone = phone
    context.code = code
    context.item_name = (pair or {}).get("item_name") or "meal"
    context.already_registered = bool(
        pair and frappe.db.exists("Customer Account", {"mobile": phone}))
    return context
