import frappe

no_cache = 1
login_required = False


def get_context(context):
    """Tour Guide PWA (/guide). Guest-allowed — auth is the WhatsApp number +
    password token scheme (guide_* APIs), not the Frappe session. Guides are
    external partners (no Frappe Users); they are invited from the POS payment
    dialog via a wa.me message carrying this page's URL and a password."""
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.csrf_token = frappe.sessions.get_csrf_token()
