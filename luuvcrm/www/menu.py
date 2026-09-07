import frappe
from frappe import _

from luuvcrm import branch
from luuvcrm.api import menu_items

no_cache = 1

def _whatsapp_number():
    """The restaurant's WhatsApp number, or "" when none is configured.

    The float button used to hard-code wa.me/9470XXXXXXX — a placeholder that has
    been live on the public menu, sending anyone who tapped it to a dead number.
    Until there is a settings doctype (see docs/SEED_DATA.md B1) this reads the
    Restaurant record, and the button simply does not render without one.
    """
    name = branch.viewing()
    if not name:
        return ""
    for field in ("whatsapp", "whatsapp_number", "phone_no", "phone"):
        if frappe.db.has_column("Restaurant", field):
            value = frappe.db.get_value("Restaurant", name, field)
            digits = "".join(c for c in str(value or "") if c.isdigit())
            if len(digits) >= 9:
                return digits if digits.startswith("94") else "94" + digits.lstrip("0")
    return ""


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True
    context.whatsapp_number = _whatsapp_number()

    restaurant_name = branch.viewing()
    if not restaurant_name:
        context.restaurant = None
        context.menu_items = []
        return

    restaurant = frappe.get_doc("Restaurant", restaurant_name)
    active_menu_name = restaurant.active_menu

    context.restaurant = restaurant
    context.menu_items = []

    if not active_menu_name:
        return

    # Cheapest first, across categories — this page is one long list a customer
    # scrolls, not the grouped grid the ordering screens show.
    context.menu_items = sorted(menu_items(restaurant_name), key=lambda x: x["rate"])
