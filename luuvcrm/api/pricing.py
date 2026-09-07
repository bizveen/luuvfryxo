"""What a thing costs: the rate-resolution chain and the service charge.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe

from luuvcrm import branch


# ─── Helpers ────────────────────────────────────────────

def _get_service_charge_rate():
    restaurant_name = branch.viewing()
    if not restaurant_name:
        return 0.0
    return float(frappe.db.get_value("Restaurant", restaurant_name, "service_charge") or 0)
def _calc_service_charge(subtotal):
    rate = _get_service_charge_rate()
    return round(subtotal * rate / 100, 2)
def _is_takeaway(order_type):
    """Take Away orders are not charged a service charge."""
    return (order_type or "").strip().lower() in ("take away", "takeaway")
def _item_is_takeaway(item_data, order_type):
    """An item carries no service charge if the whole order is take-away, or the line
    itself is flagged take-away (mixed dine-in + parcel order)."""
    return _is_takeaway(order_type) or bool(item_data.get("takeaway"))
def _service_charge_on(sc_base):
    """Service charge rate + amount for a dine-in subtotal (0 base -> 0 charge)."""
    rate = _get_service_charge_rate() if sc_base > 0 else 0.0
    return rate, round(sc_base * rate / 100, 2)
def _apply_takeaway_service_charge(pos_order):
    """Re-apply service charge on the dine-in subtotal only (excludes take-away items),
    overriding the POS Order doctype controller (which lives in the Zeloura module and is
    left untouched — it recomputes SC on the full subtotal). Call after insert/save."""
    rate = float(pos_order.get("service_charge_rate") or 0)
    subtotal = sum((i.qty or 1) * (i.rate or 0) for i in pos_order.items)
    sc_base = sum((i.qty or 1) * (i.rate or 0) for i in pos_order.items if not i.get("takeaway"))
    # A manager-approved discount applies to the items subtotal (before service charge),
    # so the SC base shrinks proportionally and the discount comes off the total.
    disc = float(pos_order.get("discount_amount") or 0)
    if disc > subtotal:
        disc = subtotal
    factor = (1 - disc / subtotal) if subtotal > 0 else 1
    sc_amount = round(sc_base * factor * rate / 100, 2) if rate > 0 else 0.0
    pos_order.db_set("service_charge_amount", sc_amount)
    pos_order.db_set("grand_total", round(subtotal - disc + sc_amount, 2))
def _sc_rate_for(order_type):
    return 0.0 if _is_takeaway(order_type) else _get_service_charge_rate()
def _sc_amount_for(subtotal, order_type):
    return 0.0 if _is_takeaway(order_type) else _calc_service_charge(subtotal)
def _get_pos_price_list():
    """Selling price list from the default POS Profile (used for price resolution)."""
    return frappe.db.get_value("POS Profile", {}, "selling_price_list")
def _resolve_item_rate(item_code, menu_rate=0, price_list=None):
    """Resolve an item's selling rate.

    Item Price is the authority: an Item Price change must reflect in the POS.
    Respects Item Price conditions — only rows whose valid_from/valid_upto window
    covers today count, customer-specific rows are skipped for general display,
    and when several rows apply the newest validity window wins.
    Priority: valid Item Price in the selling price list -> any valid selling
    Item Price -> menu row rate -> Item.standard_rate.
    """
    today = frappe.utils.today()

    def _valid_rate(with_price_list):
        cond = "AND price_list = %(price_list)s" if with_price_list else ""
        rows = frappe.db.sql(
            f"""
            SELECT price_list_rate FROM `tabItem Price`
            WHERE item_code = %(item_code)s AND selling = 1 {cond}
              AND (customer IS NULL OR customer = '')
              AND (valid_from IS NULL OR valid_from <= %(today)s)
              AND (valid_upto IS NULL OR valid_upto >= %(today)s)
            ORDER BY COALESCE(valid_from, '1900-01-01') DESC, modified DESC
            LIMIT 1
            """,
            {"item_code": item_code, "price_list": price_list, "today": today})
        return float(rows[0][0]) if rows else 0

    if price_list:
        rate = _valid_rate(True)
        if rate:
            return rate
    rate = _valid_rate(False)
    if rate:
        return rate
    menu_rate = float(menu_rate or 0)
    if menu_rate:
        return menu_rate
    return float(frappe.db.get_value("Item", item_code, "standard_rate") or 0)
def _menu_row_rates():
    """{item_code: rate} from the active Restaurant Menu, cached for the request.

    Some items are priced only on the menu row — no Item Price, no standard_rate.
    _server_item_rate needs those as its fallback or it would resolve them to 0,
    which is exactly the regression a client-supplied rate used to paper over.
    """
    cached = getattr(frappe.local, "_lf_menu_rates", None)
    if cached is not None:
        return cached
    rates = {}
    try:
        restaurant = branch.viewing()
        active_menu = frappe.db.get_value("Restaurant", restaurant, "active_menu") if restaurant else None
        if active_menu:
            for row in frappe.get_all("Restaurant Menu Item",
                                      filters={"parent": active_menu}, fields=["item", "rate"]):
                if row.item:
                    rates[row.item] = float(row.rate or 0)
    except Exception:
        # A missing or malformed menu must not stop a sale — _resolve_item_rate
        # still has Item Price and standard_rate to fall back on.
        pass
    frappe.local._lf_menu_rates = rates
    return rates
def _server_item_rate(item_code):
    """THE price of an item. Never take a rate from the client.

    Every order path used to do `rate = float(item_data.get("rate", 0))` and look
    the real price up only when that was falsy, so posting `rate: 1` for a Rs. 1600
    item billed it at Rs. 1 — through kiosk_place_order, without even logging in.

    Discounts do not come through here. They go through _apply_order_discount,
    which is approval-gated and logged, and that is the only way a line total
    should ever differ from the price list.
    """
    return _resolve_item_rate(item_code, _menu_row_rates().get(item_code, 0), _get_pos_price_list())
