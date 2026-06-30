import frappe

no_cache = 1


def get_context(context):
    # Consolidated into the unified customer app at /app. Redirect, preserving
    # any deep-link params (?phone= from WhatsApp, ?ref= from referral links).
    qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
    frappe.local.flags.redirect_location = "/app" + (("?" + qs) if qs else "")
    raise frappe.Redirect
