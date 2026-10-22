"""The self-order kiosk at the table.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe

from luuvcrm import settings
from frappe import _
from frappe.utils import now_datetime

from luuvcrm.api.loyalty import _process_cashback
from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _is_takeaway,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.promos import PROMO_PICKUP_OUTLET, PROMO_WINDOW_HOURS, _pending_promo_for
from luuvcrm.api.wa import wa_number
from luuvcrm.api.whatsapp import send_customer_whatsapp, send_order_whatsapp


# ─── Kiosk Self-Ordering ───────────────────────────────

@frappe.whitelist(allow_guest=True)
def kiosk_place_order():
    data = frappe.local.form_dict
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Online Guest"
    mobile = data.get("mobile", "").strip() or ""
    table = data.get("table", "")
    order_type = data.get("order_type", "")
    wa_confirm = str(data.get("wa_confirm") or "") in ("1", "true", "True")
    scheduled_raw = (data.get("scheduled_time") or "").strip()

    # Validate table exists — if not, silently ignore it
    if table:
        table_exists = frappe.db.get_value("Restaurant Table", table, "name")
        if not table_exists:
            table = ""

    # ── preferred time + free-item window rule (free meal must be within N hours) ──
    from frappe.utils import get_datetime, add_to_date
    scheduled_dt = None
    if scheduled_raw:
        try:
            scheduled_dt = get_datetime(scheduled_raw)
        except Exception:
            scheduled_dt = None
    # A past preferred time is meaningless → treat as ASAP (drop it).
    if scheduled_dt and scheduled_dt < now_datetime():
        scheduled_dt = None
    promo = _pending_promo_for(mobile)
    if promo and promo.get("free_item"):
        # Free meal = pickup-only at the fixed outlet → force Take Away (no delivery,
        # no dine-in table, no service charge). The customer collects at PROMO_PICKUP_OUTLET.
        order_type = "Take Away"
        table = ""
        # Preferred time must be within the next PROMO_WINDOW_HOURS. Time is optional
        # (no time = ASAP, within the window); only a time TOO FAR ahead is rejected.
        max_dt = add_to_date(now_datetime(), hours=PROMO_WINDOW_HOURS)
        if scheduled_dt and scheduled_dt > max_dt:
            return {"error": f"Free-meal orders must be within the next {PROMO_WINDOW_HOURS} hours — "
                             f"pick a time before {max_dt.strftime('%d %b %I:%M %p')}.",
                    "max_time": max_dt.strftime("%Y-%m-%d %H:%M:%S")}

    order_source = "Online"
    waiter_name = "Kiosk"

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        "restaurant_table": table,
        "order_source": order_source,
        # WhatsApp-confirmed app orders are HELD (Draft = hidden from kitchen)
        # until the customer's wa.me confirmation message arrives.
        "kitchen_status": "Draft" if wa_confirm else "Pending",
        "grand_total": 0,
        "service_charge_rate": 0,
        "service_charge_amount": 0,
        "items": [],
    })
    subtotal = 0
    sc_base = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = _server_item_rate(item_code)
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    # QR promo: registered sticker code + campaign free item → add at Rs 0 (first order)
    promo_item_name = ""
    if promo and promo.get("free_item"):
        fi = promo["free_item"]
        promo_item_name = frappe.db.get_value("Item", fi, "item_name") or fi
        pos_order.append("items", {"item": fi, "item_name": promo_item_name, "qty": 1,
                                   "rate": 0, "takeaway": 1 if _is_takeaway(order_type) else 0})

    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    _apply_takeaway_service_charge(pos_order)
    if scheduled_dt and frappe.db.has_column("POS Order", "scheduled_time"):
        pos_order.db_set("scheduled_time", scheduled_dt)
    if wa_confirm:
        pos_order.db_set("whatsapp_status", "Awaiting Confirmation")
    frappe.db.commit()

    # single use: mark the promo code redeemed against this order
    if promo and promo.get("free_item"):
        try:
            frappe.db.set_value("QR Promo Code", promo["name"],
                                {"status": "Redeemed", "redeemed_on": now_datetime(),
                                 "pos_order": pos_order.name})
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Promo redeem error: {str(e)}", "QR Promo")

    if not wa_confirm:
        # instant orders (in-store kiosk): notify + cashback right away.
        # wa_confirm orders run these on the customer's WhatsApp confirmation.
        if mobile and pos_order.grand_total > 0:
            try:
                _process_cashback(mobile, pos_order.name, pos_order.grand_total)
            except Exception as e:
                frappe.log_error(f"Cashback error in kiosk: {str(e)}", "Cashback")
        try:
            send_order_whatsapp(pos_order.name)
        except Exception as e:
            frappe.log_error(f"Restaurant WhatsApp error: {str(e)}", "WhatsApp")
        if mobile:
            try:
                send_customer_whatsapp(pos_order.name, mobile)
            except Exception as e:
                frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")

    confirm_link = ""
    if wa_confirm:
        from urllib.parse import quote
        confirm_link = ("https://wa.me/" + wa_number() + "?text="
                        + quote(f"Confirming my order #{pos_order.name} ✅"))

    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "table": table,
        "customer_name": customer_name,
        "mobile": mobile,
        "promo_item": promo_item_name,
        "pickup_outlet": settings.get("pickup_outlet") if promo_item_name else "",
        "scheduled_time": scheduled_dt.strftime("%Y-%m-%d %H:%M:%S") if scheduled_dt else "",
        "needs_wa_confirm": wa_confirm,
        "confirm_link": confirm_link,
    }
# ─── Kiosk Setup ─────────────────────────────────────────

@frappe.whitelist()
def setup_kiosk_tools():
    """One-time setup: create Item Review DocType + custom fields on Item."""
    frappe.only_for("System Manager")
    if frappe.db.exists("DocType", "Item Review"):
        return {"status": "already_exists"}

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Item Review",
        "module": "Zeloura",
        "custom": 1,
        "fields": [
            {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
            {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
            {"fieldname": "rating", "label": "Rating", "fieldtype": "Rating", "reqd": 1, "in_list_view": 1},
            {"fieldname": "comment", "label": "Comment", "fieldtype": "Small Text"},
            {"fieldname": "submitted_by", "label": "Submitted By", "fieldtype": "Data"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Guest", "read": 1, "write": 1, "create": 1},
        ],
    })
    doc.insert()

    fields = [
        {"dt": "Item", "fieldname": "speciality_tags", "label": "Speciality Tags", "fieldtype": "Small Text", "description": "Comma-separated: Chef's Special, Most Popular, Spicy, Vegan, Gluten-Free"},
        {"dt": "Item", "fieldname": "youtube_url", "label": "YouTube Video URL", "fieldtype": "Data"},
        {"dt": "Item", "fieldname": "prep_time", "label": "Preparation Time (mins)", "fieldtype": "Int"},
    ]
    for f in fields:
        if not frappe.db.exists("Custom Field", {"dt": f["dt"], "fieldname": f["fieldname"]}):
            cf = frappe.get_doc({
                "doctype": "Custom Field",
                "dt": f["dt"],
                "fieldname": f["fieldname"],
                "label": f["label"],
                "fieldtype": f["fieldtype"],
                "description": f.get("description", ""),
                "insert_after": "image",
            })
            cf.insert()

    return {"status": "created"}
