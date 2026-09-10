import frappe

from luuvcrm import roles
from luuvcrm.api import tables as api_tables

no_cache = 1
no_sitemap = 1

def get_context(context):
    """Printable kiosk QR labels (/qrcodes). Manager-only.

    It was public, and it is a directory: every table in the restaurant and the
    kiosk URL that orders against it. Anyone who loaded it could order to any
    table without being in the building.
    """
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/pos-login?redirect=/qrcodes"
        raise frappe.Redirect

    roles.require_manager()

    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    base_url = frappe.utils.get_url()
    # A sticker for a table nobody sits at is a sticker somebody prints and
    # glues to nothing.
    tables = api_tables.for_picker()
    qr_list = []
    for t in tables:
        qr_list.append({
            "name": t.name,
            "url": base_url + "/kiosk?table=" + t.name,
        })
    context.qr_list = qr_list
    context.base_url = base_url

