import frappe
from frappe import _

from luuvcrm import branch
from luuvcrm.api import _resolve_item_rate, _get_pos_price_list

no_cache = 1

def _whatsapp_number():
    """The restaurant's WhatsApp number, or "" when none is configured.

    The float button used to hard-code wa.me/9470XXXXXXX — a placeholder that has
    been live on the public menu, sending anyone who tapped it to a dead number.
    Until there is a settings doctype (see docs/SEED_DATA.md B1) this reads the
    Restaurant record, and the button simply does not render without one.
    """
    name = branch.current()
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

    restaurant_name = branch.current()
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

    menu = frappe.get_doc("Restaurant Menu", active_menu_name)
    if not menu.enabled:
        return

    price_list = _get_pos_price_list()

    for item_row in menu.items:
        item_code = item_row.item
        if not item_code:
            continue

        item_doc = frappe.get_doc("Item", item_code)
        context.menu_items.append({
            "code": item_code,
            "name": item_doc.item_name or item_code,
            "description": item_doc.description or "",
            "image": item_doc.image or "",
            "rate": _resolve_item_rate(item_code, item_row.rate, price_list),
            "group": item_doc.item_group or "General",
        })

    context.menu_items.sort(key=lambda x: x.get("rate", 0))
