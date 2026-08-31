import frappe

no_cache = 1


def get_context(context):
    # The customer PWA moved /customer → /cus (2026-07-12). Keep /customer as a
    # redirect alias so the ~1h of WhatsApp ?phone= links sent while it was live
    # still work. Same pattern as myorders.py (website-render path emits a proper
    # 301 Location). Preserve the query string (?phone= / ?ref= / ?promo=).
    qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
    frappe.local.flags.redirect_location = "/cus" + (("?" + qs) if qs else "")
    raise frappe.Redirect
