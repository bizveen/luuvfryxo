import frappe
from frappe import _
import json
import secrets
from frappe.utils import now_datetime
import random
import string

# ─── Helpers ────────────────────────────────────────────

def _get_service_charge_rate():
    restaurant_name = frappe.get_value("Restaurant", {}, "name")
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
    Priority: Item Price in the selling price list -> any selling Item Price
    -> menu row rate -> Item.standard_rate.
    """
    if price_list:
        pl_rate = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": price_list, "selling": 1},
            "price_list_rate",
        )
        if pl_rate:
            return float(pl_rate)
    pl_rate = frappe.db.get_value(
        "Item Price", {"item_code": item_code, "selling": 1}, "price_list_rate"
    )
    if pl_rate:
        return float(pl_rate)
    menu_rate = float(menu_rate or 0)
    if menu_rate:
        return menu_rate
    return float(frappe.db.get_value("Item", item_code, "standard_rate") or 0)

# POS roles allowed to operate shifts (mirrors www/pos.py page gate).
POS_ROLES = {"System Manager", "POS User", "Sales User", "Cashier", "Administrator"}

def _require_pos_role():
    """Guard: only POS staff may operate shifts. Raises PermissionError otherwise."""
    if not (set(frappe.get_roles()) & POS_ROLES):
        frappe.throw(_("You are not permitted to operate POS shifts"), frappe.PermissionError)

def _resolve_pos_profile_name(requested=""):
    """POS Profile from request, else the first configured profile."""
    return requested or frappe.db.get_value("POS Profile", {}, "name")

def _link_invoice_to_order(invoice_name, order_name):
    """Set the reverse invoice -> POS Order link (POS Invoice or Sales Invoice)."""
    dt = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
    if frappe.db.has_column(dt, "pos_order"):
        frappe.db.set_value(dt, invoice_name, "pos_order", order_name)

def _active_pos_profile_name(requested=""):
    """POS Profile of the session user's OPEN shift (so POS Invoices match the opening
    entry for native consolidation), else requested, else first profile."""
    prof = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "pos_profile")
    return prof or _resolve_pos_profile_name(requested)

def _invoice_doctype_for(pos_profile_name):
    """POS sales always become native POS Invoices (open/close entries + consolidation).
    An open shift is guaranteed by _ensure_open_shift() before any invoice is created."""
    return "POS Invoice"

def _ensure_open_shift(pos_profile_name):
    """Guarantee the session user has an open shift for the profile; auto-open one
    (0 opening per payment mode) if not. POS Invoice creation requires an open POS
    Opening Entry, so this keeps every cashier sale on the native POS pipeline."""
    user = frappe.session.user
    existing = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "docstatus": 1, "user": user, "pos_profile": pos_profile_name}, "name")
    if existing:
        return existing
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    opening = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "pos_profile": pos_profile_name,
        "company": pos_profile.company,
        "period_start_date": now_datetime(),
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "user": user,
        "balance_details": [{"mode_of_payment": m, "opening_amount": 0}
                            for m in _profile_payment_modes(pos_profile_name)],
    })
    opening.flags.ignore_permissions = True
    opening.insert()
    opening.submit()
    return opening.name

def _get_invoice_doc(invoice_name):
    """Load an order's invoice regardless of whether it is a POS Invoice or Sales Invoice."""
    dt = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
    return frappe.get_doc(dt, invoice_name)

def _service_charge_account(company):
    """Income account the restaurant service charge posts to.
    The service charge is a charge/tax on the bill, not a sellable item, so it goes
    into the invoice's Sales Taxes and Charges table against an income account."""
    abbr = frappe.get_cached_value("Company", company, "abbr")
    for nm in (f"Service Charge - {abbr}", f"Service - {abbr}"):
        if frappe.db.exists("Account", nm):
            return nm
    return frappe.get_cached_value("Company", company, "default_income_account")


def _append_service_charge_tax(invoice, sc_amount):
    """Add the service charge as an Actual Sales Taxes and Charges row (not a line item).
    sc_amount is the pre-computed dine-in-only amount, so it's added verbatim."""
    invoice.append("taxes", {
        "charge_type": "Actual",
        "account_head": _service_charge_account(invoice.company),
        "description": "Service Charge",
        "tax_amount": sc_amount,
        "category": "Total",
        "add_deduct_tax": "Add",
    })

def _ensure_order_type_field():
    if not frappe.db.exists("Custom Field", {"dt": "POS Order", "fieldname": "order_type"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "POS Order",
            "fieldname": "order_type",
            "label": "Order Type",
            "fieldtype": "Select",
            "options": "Dine In\nTake Away\nDelivery",
            "insert_after": "order_source",
        })
        cf.flags.ignore_permissions = True
        cf.insert()

# ─── POS Profiles ────────────────────────────────────────

@frappe.whitelist()
def get_pos_profiles():
    profiles = frappe.get_all("POS Profile", fields=["name", "company", "warehouse", "currency"])
    modes = frappe.get_all("Mode of Payment", fields=["name"], order_by="name asc")
    return {"profiles": profiles, "payment_modes": modes}

# ─── Tables ──────────────────────────────────────────────

@frappe.whitelist()
def get_tables():
    tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    return {"tables": tables}

@frappe.whitelist()
def get_tables_with_status():
    active = frappe.get_all("POS Order",
        filters=[
            ["docstatus", "=", 0],
            ["kitchen_status", "!=", "Served"],
            ["pos_invoice", "is", "not set"],
        ],
        fields=["restaurant_table", "name", "kitchen_status"],
    )
    seen = {}
    tables = []
    for o in active:
        if o.restaurant_table and o.restaurant_table not in seen:
            seen[o.restaurant_table] = True
            tables.append({
                "name": o.restaurant_table,
                "active": True,
                "order_name": o.name,
                "order_status": o.kitchen_status,
            })
    return {"tables": tables}

# ─── Place Order ─────────────────────────────────────────

def _set_cash_tender(invoice, payment_mode, tendered, grand_total):
    """Record cash tendered + change returned on the invoice for the receipt.
    Display-only custom fields (custom_tendered_amount / custom_change_returned) —
    no effect on the payment rows, paid_amount, or any GL/accounting."""
    try:
        t = float(tendered or 0)
    except (TypeError, ValueError):
        t = 0
    if payment_mode == "Cash" and t > (grand_total or 0):
        invoice.custom_tendered_amount = t
        invoice.custom_change_returned = round(t - grand_total, 2)


@frappe.whitelist(methods=["POST"])
def place_order():
    data = frappe.local.form_dict

    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Walk-in"
    mobile = data.get("mobile", "").strip() or ""
    table = data.get("table", "")
    notes = data.get("notes", "").strip()
    order_source = data.get("order_source", "Walk-in")
    order_type = data.get("order_type", "")
    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))
    waiter_name = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    _ensure_order_type_field()

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)
    invoice_items = []
    pos_items = []
    subtotal = 0
    sc_base = 0

    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)

        invoice_items.append({"item_code": item_code, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        pos_items.append({"item": item_code, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    grand_total = subtotal + sc_amount

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": grand_total})

    invoice = frappe.get_doc({
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order | Customer: {customer_name} | Phone: {mobile} | Table: {table}",
        "items": [],
        "payments": payments,
    })
    for inv_item in invoice_items:
        invoice.append("items", inv_item)

    if sc_amount > 0:
        _append_service_charge_tax(invoice, sc_amount)
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), grand_total)

    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()  # finalize the sale so it consolidates at shift close

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        "restaurant_table": table,
        "order_source": order_source,
        "kitchen_status": "Pending",
        "grand_total": grand_total,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "pos_invoice": invoice.name,
        "notes": notes,
        "items": [],
    })

    opening_entry_name = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")
    if opening_entry_name:
        pos_order.pos_opening_entry = opening_entry_name

    for item in pos_items:
        item_name = frappe.db.get_value("Item", item["item"], "item_name") or item["item"]
        pos_order.append("items", {
            "item": item["item"],
            "item_name": item_name,
            "qty": item["qty"],
            "rate": item["rate"],
            "takeaway": item.get("takeaway", 0),
        })

    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)
    _apply_takeaway_service_charge(pos_order)

    _link_invoice_to_order(invoice.name, pos_order.name)

    return {
        "name": pos_order.name,
        "invoice_name": invoice.name,
        "grand_total": grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "table": table,
        "payment_mode": payment_mode,
        "customer_name": customer_name,
        "mobile": mobile,
        "order_type": order_type,
    }

# ─── Send to Kitchen (order only, no invoice) ────────────

@frappe.whitelist(methods=["POST"])
def send_to_kitchen():
    data = frappe.local.form_dict
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Walk-in"
    mobile = data.get("mobile", "").strip() or ""
    table = data.get("table", "")
    notes = data.get("notes", "").strip()
    order_source = data.get("order_source", "Walk-in")
    order_type = data.get("order_type", "")
    amended_from = data.get("amended_from", "").strip()
    waiter_name = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    # Draft = auto-created as the cashier builds the ticket; not yet fired to the kitchen.
    is_draft = int(data.get("draft") or 0)

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        "restaurant_table": table,
        "order_source": order_source,
        "kitchen_status": "Draft" if is_draft else "Pending",
        "grand_total": 0,
        "service_charge_rate": 0,
        "service_charge_amount": 0,
        "notes": notes,
        "items": [],
    })

    opening_entry_name = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")
    if opening_entry_name:
        pos_order.pos_opening_entry = opening_entry_name

    subtotal = 0
    sc_base = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)
    _apply_takeaway_service_charge(pos_order)

    # If this order was created from an amend, create Version records linking both orders
    if amended_from:
        old_doc = frappe.get_doc("POS Order", amended_from) if frappe.db.exists("POS Order", amended_from) else None
        old_status = old_doc.kitchen_status if old_doc else "Unknown"
        # Version on the NEW order: points back to the cancelled order
        version = frappe.get_doc({
            "doctype": "Version",
            "ref_doctype": "POS Order",
            "docname": pos_order.name,
            "data": frappe.as_json({
                "changed": [
                    ["amended_from", "", amended_from],
                    ["note", "", f"Re-created from amended order #{amended_from} (was {old_status})"]
                ]
            }),
            "owner": frappe.session.user,
            "modified_by": frappe.session.user,
        })
        version.flags.ignore_permissions = True
        version.insert()
        # Version on the CANCELLED order: points to the new order
        ver2 = frappe.get_doc({
            "doctype": "Version",
            "ref_doctype": "POS Order",
            "docname": amended_from,
            "data": frappe.as_json({
                "changed": [
                    ["amended_to", "", pos_order.name],
                    ["note", "", f"Re-created as #{pos_order.name}"]
                ]
            }),
            "owner": frappe.session.user,
            "modified_by": frappe.session.user,
        })
        ver2.flags.ignore_permissions = True
        ver2.insert()

    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "table": table,
        "customer_name": customer_name,
        "mobile": mobile,
        "order_type": order_type,
    }

# ─── Process Payment (create invoice from existing order) ─

@frappe.whitelist(methods=["POST"])
def process_payment():
    data = frappe.local.form_dict
    order_name = data.get("order_name")

    if not order_name:
        frappe.throw(_("Order name is required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not in draft state"))
    if pos_order.pos_invoice:
        frappe.throw(_("Payment already processed for this order"))
    if any(int(i.get("paid_qty") or 0) > 0 for i in pos_order.items):
        frappe.throw(_("This order has split payments. Use Split Bill to pay the remaining items."))

    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))

    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": pos_order.grand_total})

    invoice = frappe.get_doc({
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order: {order_name} | {payment_mode}",
        "items": [],
        "payments": payments,
    })

    for item in pos_order.items:
        invoice.append("items", {"item_code": item.item, "qty": item.qty, "rate": item.rate, "takeaway": int(item.get("takeaway") or 0)})

    if pos_order.service_charge_amount and pos_order.service_charge_amount > 0:
        _append_service_charge_tax(invoice, pos_order.service_charge_amount)
    if pos_order.get("discount_amount") and pos_order.discount_amount > 0:
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = pos_order.discount_amount
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), pos_order.grand_total)

    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()  # finalize the sale so it consolidates at shift close

    pos_order.db_set("pos_invoice", invoice.name)
    _link_invoice_to_order(invoice.name, order_name)

    return {
        "name": order_name,
        "invoice_name": invoice.name,
        "grand_total": pos_order.grand_total,
        "service_charge_rate": pos_order.service_charge_rate or 0,
        "service_charge_amount": pos_order.service_charge_amount or 0,
        "payment_mode": payment_mode,
    }

# ─── Split Bill (pay a subset of an order's items as its own bill) ─

@frappe.whitelist(methods=["POST"])
def pay_split():
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    lines_raw = data.get("lines")
    lines = frappe.parse_json(lines_raw) if isinstance(lines_raw, str) else (lines_raw or [])

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not open"))

    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))

    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)

    # One order row per item; validate requested qty against remaining (qty - paid_qty).
    order_rows = {it.item: it for it in pos_order.items}
    requested = {}
    for ln in lines:
        code = ln.get("item")
        qty = int(ln.get("qty") or 0)
        if qty <= 0:
            continue
        row = order_rows.get(code)
        if not row:
            frappe.throw(_("Item {0} is not on this order").format(code))
        remaining = int(row.qty or 0) - int(row.get("paid_qty") or 0)
        if requested.get(code, 0) + qty > remaining:
            frappe.throw(_("Only {0} of {1} left to bill").format(remaining, code))
        requested[code] = requested.get(code, 0) + qty
    if not requested:
        frappe.throw(_("Select at least one item to bill"))

    sc_rate = _get_service_charge_rate()
    subtotal = 0
    sc_base = 0
    invoice_items = []
    for code, qty in requested.items():
        row = order_rows[code]
        rate = float(row.rate or 0)
        invoice_items.append({"item_code": code, "qty": qty, "rate": rate, "takeaway": 1 if row.get("takeaway") else 0})
        subtotal += rate * qty
        if not row.get("takeaway"):
            sc_base += rate * qty

    sc_amount = round(sc_base * sc_rate / 100, 2) if (sc_rate > 0 and sc_base > 0) else 0.0
    grand_total = subtotal + sc_amount

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": grand_total})

    invoice = frappe.get_doc({
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order: {order_name} | SPLIT | {payment_mode}",
        "items": [],
        "payments": payments,
    })
    for inv_item in invoice_items:
        invoice.append("items", inv_item)
    if sc_amount > 0:
        _append_service_charge_tax(invoice, sc_amount)
    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()

    for code, qty in requested.items():
        row = order_rows[code]
        row.db_set("paid_qty", int(row.get("paid_qty") or 0) + qty)

    pos_order.reload()
    fully_paid = _order_fully_paid(pos_order)
    if fully_paid:
        if not pos_order.pos_invoice:
            pos_order.db_set("pos_invoice", invoice.name)
        _link_invoice_to_order(invoice.name, order_name)
        pos_order.db_set("docstatus", 1)
        pos_order.db_set("kitchen_status", "Served")

    return {
        "name": order_name,
        "invoice_name": invoice.name,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate if sc_amount > 0 else 0,
        "service_charge_amount": sc_amount,
        "grand_total": grand_total,
        "payment_mode": payment_mode,
        "fully_paid": fully_paid,
    }

# ─── Ongoing Orders ──────────────────────────────────────

@frappe.whitelist()
def get_ongoing_orders():
    orders = frappe.get_all("POS Order",
        filters={"docstatus": 0, "order_source": ["in", ["Walk-in", "Waiter"]]},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice", "order_type",
                "discount_amount", "discount_note"],
        order_by="creation desc"
    )
    # Exclude orders that already have an invoice (already paid)
    orders = [o for o in orders if not o.pos_invoice]
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        doc = frappe.get_doc("POS Order", o.name)
        o["items_count"] = len(doc.get("items") or [])
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate, "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0)} for i in (doc.get("items") or [])])
    return {"orders": orders}

# ─── Kiosk Orders (Online source) ────────────────────────

@frappe.whitelist()
def get_kiosk_orders():
    orders = frappe.get_all("POS Order",
        filters={"docstatus": 0, "order_source": "Online"},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice"],
        order_by="creation desc"
    )
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        doc = frappe.get_doc("POS Order", o.name)
        o["items_count"] = len(doc.get("items") or [])
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate, "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0)} for i in (doc.get("items") or [])])
    return {"orders": orders}

# ─── Completed Orders (current shift only) ──────────────

@frappe.whitelist()
def get_completed_orders():
    opening_name = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")

    filters = {"docstatus": 1, "order_source": ["in", ["Walk-in", "Waiter"]]}
    if opening_name:
        filters["pos_opening_entry"] = opening_name

    orders = frappe.get_all("POS Order",
        filters=filters,
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "modified", "order_source", "pos_invoice", "order_type",
                "kitchen_status"],
        order_by="modified desc",
        limit_page_length=100
    )
    for o in orders:
        o["completed_at"] = frappe.utils.pretty_date(o["modified"])
        doc = frappe.get_doc("POS Order", o.name)
        o["items_count"] = len(doc.get("items") or [])
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])])
        # Check if cancelled order was re-created (fraud prevention)
        if o.kitchen_status == "Cancelled":
            versions = frappe.get_all("Version",
                filters={"ref_doctype": "POS Order", "docname": o.name},
                fields=["data"], order_by="creation desc", limit_page_length=5
            )
            for v in versions:
                if v.data:
                    try:
                        d = frappe.parse_json(v.data)
                        for field, old, new in d.get("changed") or []:
                            if field == "amended_to":
                                o["recreated_as"] = new
                                break
                    except Exception:
                        pass
    return {"orders": orders}

# ─── Cancel Order ────────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def cancel_order():
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot cancel — payment already processed"))
    old_kitchen_status = pos_order.kitchen_status
    pos_order.db_set("docstatus", 2)
    pos_order.db_set("kitchen_status", "Cancelled")
    # Manually create a Version record so Changes tab shows this
    from frappe.utils import now_datetime
    version = frappe.get_doc({
        "doctype": "Version",
        "ref_doctype": "POS Order",
        "docname": order_name,
        "data": frappe.as_json({
            "changed": [
                ["docstatus", "0", "2"],
                ["kitchen_status", old_kitchen_status or "Pending", "Cancelled"]
            ]
        }),
        "owner": frappe.session.user,
        "modified_by": frappe.session.user,
        "creation": now_datetime(),
    })
    version.flags.ignore_permissions = True
    version.insert()
    return {"status": "cancelled", "name": order_name}

# ─── All Changelogs (recent changes across orders) ──────

@frappe.whitelist()
def get_all_changelogs():
    versions = frappe.get_all("Version",
        filters={"ref_doctype": "POS Order", "creation": [">=", frappe.utils.today()]},
        fields=["name", "creation", "owner", "data", "docname"],
        order_by="creation desc",
        limit_page_length=50
    )
    logs = []
    for v in versions:
        entry = {
            "order_name": v.docname,
            "created": str(v.creation),
            "time_ago": frappe.utils.pretty_date(v.creation),
            "owner": v.owner,
            "changes": []
        }
        if v.data:
            try:
                d = frappe.parse_json(v.data)
                for field, old, new in d.get("changed") or []:
                    entry["changes"].append({"field": field, "old": str(old or ""), "new": str(new or "")})
                if d.get("added"):
                    entry["changes"].append({"field": "items", "action": "added", "count": len(d["added"])})
                if d.get("removed"):
                    entry["changes"].append({"field": "items", "action": "removed", "count": len(d["removed"])})
                if d.get("row_changed"):
                    entry["changes"].append({"field": "items", "action": "modified", "count": len(d["row_changed"])})
            except Exception:
                pass
        if entry["changes"]:
            logs.append(entry)
    return {"logs": logs}

# ─── Mark Order as Served ────────────────────────────────

@frappe.whitelist(methods=["POST"])
def mark_order_served():
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not in draft state"))
    if pos_order.kitchen_status == "Served":
        frappe.throw(_("Order already served"))

    if pos_order.pos_invoice:
        invoice = _get_invoice_doc(pos_order.pos_invoice)
        if invoice.docstatus == 0:
            invoice.flags.ignore_permissions = True
            invoice.submit()

    pos_order.db_set("docstatus", 1)
    pos_order.db_set("kitchen_status", "Served")

    return {"name": order_name, "status": "Served", "invoice_name": pos_order.pos_invoice}

# ─── POS Shift (ERPNext POS Opening/Closing Entry) ──────

def _profile_payment_modes(pos_profile_name):
    """Ordered mode-of-payment names configured on a POS Profile (Cash first if present)."""
    modes = frappe.get_all("POS Payment Method",
        filters={"parent": pos_profile_name}, fields=["mode_of_payment"], order_by="idx")
    names = [m.mode_of_payment for m in modes if m.mode_of_payment]
    return names or ["Cash"]

@frappe.whitelist(methods=["POST"])
def pos_open_shift():
    _require_pos_role()
    data = frappe.local.form_dict
    pos_profile_name = _resolve_pos_profile_name(data.get("pos_profile", ""))

    existing = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")
    if existing:
        frappe.throw(_("A shift is already open. Close it first."))

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)

    # Per-mode opening amounts (native). Accept posted balance_details, else fall back to
    # the profile's payment methods at 0 (legacy single opening_balance honoured for Cash).
    posted = data.get("balance_details")
    amounts = {}
    if posted:
        try:
            for row in json.loads(posted):
                if row.get("mode_of_payment"):
                    amounts[row["mode_of_payment"]] = float(row.get("opening_amount") or 0)
        except (ValueError, TypeError):
            amounts = {}
    if not amounts and data.get("opening_balance") is not None:
        amounts["Cash"] = float(data.get("opening_balance") or 0)

    balance_details = [
        {"mode_of_payment": mode, "opening_amount": amounts.get(mode, 0)}
        for mode in _profile_payment_modes(pos_profile_name)
    ]

    opening = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "pos_profile": pos_profile_name,
        "company": pos_profile.company,
        "period_start_date": now_datetime(),
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "user": frappe.session.user,
        "balance_details": balance_details,
    })
    opening.insert()
    opening.submit()

    opening_amt = sum(float(d["opening_amount"]) for d in balance_details)
    return {
        "shift": {
            "status": "open",
            "name": opening.name,
            "pos_profile": pos_profile_name,
            "opening_balance": opening_amt,
            "opening_details": balance_details,
            "total_sales": 0,
            "order_count": 0,
            "payment_breakdown": {},
            "period_start": str(opening.period_start_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }

def _consolidate_shift_inline(closing):
    """Merge a POS Closing Entry's POS Invoices into Sales Invoice(s) RIGHT NOW, in this process.

    ERPNext's consolidate_pos_invoices() enqueues this onto the background queue whenever a shift
    has >= 10 POS Invoices. This bench's queue workers can't import an installed app, so that queued
    job dies — the closing is left status="Queued"/"Failed" and the opening stuck "Open" (the cashier
    can then neither open a new shift nor log out). Calling create_merge_logs() directly in the
    backend, where imports work, does the exact same consolidation reliably. Idempotent-ish: skip
    when the closing already consolidated (status "Submitted")."""
    from erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log import (
        get_invoice_customer_map,
        create_merge_logs,
    )
    invoices = closing.get("pos_transactions") or []
    if not invoices:
        # Nothing to merge — just flip the opening to Closed.
        closing.update_opening_entry()
        closing.set_status(update=True, status="Submitted")
        return
    invoice_by_customer = get_invoice_customer_map(invoices)
    create_merge_logs(invoice_by_customer, closing)  # creates Sales Invoice(s) + marks opening Closed


@frappe.whitelist(methods=["POST"])
def pos_close_shift():
    _require_pos_role()
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    data = frappe.local.form_dict

    # Per-mode counted amounts (native). Accept posted closing_details, else fall back to the
    # legacy single closing_balance (applied to Cash).
    counted = {}
    posted = data.get("closing_details")
    if posted:
        try:
            for row in json.loads(posted):
                if row.get("mode_of_payment"):
                    counted[row["mode_of_payment"]] = float(row.get("closing_amount") or 0)
        except (ValueError, TypeError):
            counted = {}
    if not counted and data.get("closing_balance") is not None:
        counted["Cash"] = float(data.get("closing_balance") or 0)

    opening_name = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name")
    if not opening_name:
        frappe.throw(_("No open shift found"))

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    # Native: gather this shift's submitted POS Invoices into a POS Closing Entry
    # (pos_transactions + taxes + payment reconciliation built by ERPNext).
    closing = make_closing_entry_from_opening(opening)

    # Apply the cashier's counted amount per mode; trust expected where none counted.
    payment_breakdown = {}
    for row in closing.payment_reconciliation:
        expected = float(row.expected_amount or 0)
        opening_amt_row = float(row.opening_amount or 0)
        row.closing_amount = counted.get(row.mode_of_payment, expected)
        row.difference = float(row.closing_amount) - expected
        payment_breakdown[row.mode_of_payment] = expected - opening_amt_row

    closing.flags.ignore_permissions = True
    closing.insert()
    closing.submit()  # docstatus=1; ERPNext consolidates inline for <10 invoices, else enqueues
    closing.reload()

    # For a big shift (>= 10 invoices) ERPNext only QUEUED the consolidation — and this bench's
    # queue worker can't run it, which is exactly what left shifts stuck "Open". Finish it here.
    consolidated = True
    if closing.status == "Queued":
        # Flip the opening to Closed up front so the cashier is unwedged even if the heavy merge
        # below is slow/fails (they can always open a fresh shift or log out). Committed first so a
        # later rollback of a failed merge can't reopen the shift.
        opening.db_set("status", "Closed")
        opening.db_set("pos_closing_entry", closing.name)
        frappe.db.commit()
        try:
            _consolidate_shift_inline(closing)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "POS shift consolidation failed")
            consolidated = False

    opening_amt = sum(float(d.opening_amount or 0) for d in opening.balance_details)
    total_sales = float(closing.grand_total or 0)
    order_count = len(closing.pos_transactions or [])

    return {
        "shift": {
            "status": "closed",
            "name": closing.name,
            "opening_name": opening_name,
            "opening_balance": opening_amt,
            "total_sales": total_sales,
            "order_count": order_count,
            "consolidated": consolidated,
            "payment_breakdown": payment_breakdown,
            "payment_reconciliation": [{
                "mode_of_payment": r.mode_of_payment,
                "opening_amount": float(r.opening_amount or 0),
                "expected_amount": float(r.expected_amount or 0),
                "closing_amount": float(r.closing_amount or 0),
                "difference": float(r.difference or 0),
            } for r in closing.payment_reconciliation],
            "period_start": str(closing.period_start_date),
            "period_end": str(closing.period_end_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }

@frappe.whitelist()
def get_shift_closing_data():
    """Return shift closing data for re-printing summary."""
    _require_pos_role()
    closing_name = frappe.local.form_dict.get("closing_name", "")
    if not closing_name:
        frappe.throw(_("Closing name is required"))
    try:
        closing = frappe.get_doc("POS Closing Entry", closing_name)
    except frappe.DoesNotExistError:
        frappe.throw(_("Closing entry not found"))

    opening_amt = 0
    for d in (closing.get("payment_reconciliation") or []):
        opening_amt += float(d.opening_amount)

    payment_breakdown = {}
    payment_reconciliation = []
    for d in (closing.get("payment_reconciliation") or []):
        payment_breakdown[d.mode_of_payment] = float(d.closing_amount) - float(d.opening_amount)
        payment_reconciliation.append({
            "mode_of_payment": d.mode_of_payment,
            "opening_amount": float(d.opening_amount),
            "expected_amount": float(d.expected_amount),
            "closing_amount": float(d.closing_amount),
            "difference": float(d.difference)
        })

    return {
        "shift": {
            "status": "closed",
            "name": closing.name,
            "opening_balance": opening_amt,
            "total_sales": closing.grand_total,
            "net_total": closing.net_total,
            "order_count": 0,
            "payment_breakdown": payment_breakdown,
            "payment_reconciliation": payment_reconciliation,
            "period_start": str(closing.period_start_date),
            "period_end": str(closing.period_end_date),
            "cashier": closing.user
        }
    }


@frappe.whitelist(methods=["POST"])
def recover_stuck_shift(opening=None):
    """Force-finish a shift left stuck "Open" because its consolidation failed/queued (e.g. a big
    shift whose merge was sent to a dead queue worker). Cancels any half-done Failed/Queued closings
    for the opening, builds a fresh closing from its still-unconsolidated invoices, consolidates
    inline, and marks the opening Closed. Safe to call on any of the user's stuck shifts (or pass an
    explicit opening name for a manager to recover a cashier's shift)."""
    _require_pos_role()
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    opening_name = opening or frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name")
    if not opening_name:
        frappe.throw(_("No stuck shift found"))
    opening_doc = frappe.get_doc("POS Opening Entry", opening_name)

    # Drop earlier half-done closings (Failed/Queued) — nothing was consolidated, so cancelling is
    # safe and lets us build one clean closing over the current invoices. Best-effort.
    for c in frappe.get_all("POS Closing Entry",
            filters={"pos_opening_entry": opening_name, "docstatus": 1,
                     "status": ["in", ["Failed", "Queued"]]}, pluck="name"):
        try:
            cdoc = frappe.get_doc("POS Closing Entry", c)
            cdoc.flags.ignore_permissions = True
            cdoc.cancel()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "recover_stuck_shift: cancel old closing")
    frappe.db.commit()

    closing = make_closing_entry_from_opening(opening_doc)
    for row in closing.payment_reconciliation:
        # On recovery we trust the system-expected amounts (the cashier's count was lost with the
        # failed close); a manager can adjust later if needed.
        row.closing_amount = float(row.expected_amount or 0)
        row.difference = 0
    closing.flags.ignore_permissions = True
    closing.insert()
    closing.submit()
    closing.reload()

    consolidated = closing.status != "Queued"
    if closing.status == "Queued":
        opening_doc.db_set("status", "Closed")
        opening_doc.db_set("pos_closing_entry", closing.name)
        frappe.db.commit()
        try:
            _consolidate_shift_inline(closing)
            frappe.db.commit()
            consolidated = True
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "recover_stuck_shift: consolidation failed")
            consolidated = False

    return {
        "recovered": True,
        "opening": opening_name,
        "closing": closing.name,
        "invoices": len(closing.pos_transactions or []),
        "consolidated": consolidated,
    }


@frappe.whitelist()
def get_pos_shift():
    _require_pos_role()
    opening_name = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name")
    if not opening_name:
        return {"shift": None}

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    opening_amt = 0
    opening_details = []
    for d in opening.balance_details:
        opening_amt += float(d.opening_amount)
        opening_details.append({"mode_of_payment": d.mode_of_payment,
                                "opening_amount": float(d.opening_amount or 0)})

    # Live totals from this shift's submitted POS Invoices — the exact set the close
    # consolidates (so the close modal shows the complete sales, incl. paid-not-served).
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import get_pos_invoices
    invoices = get_pos_invoices(opening.period_start_date, now_datetime(),
                                opening.pos_profile, opening.user)
    total_sales = sum(float(i.get("grand_total") or 0) for i in invoices)
    order_count = len(invoices)

    payment_breakdown = {}
    for inv in invoices:
        for p in (inv.get("payments") or []):
            mode = p.get("mode_of_payment")
            if mode:
                payment_breakdown[mode] = payment_breakdown.get(mode, 0) + float(p.get("amount") or 0)

    return {
        "shift": {
            "status": "open",
            "name": opening_name,
            "pos_profile": opening.pos_profile,
            "opening_balance": opening_amt,
            "opening_details": opening_details,
            "total_sales": total_sales,
            "order_count": order_count,
            "payment_breakdown": payment_breakdown,
            "period_start": str(opening.period_start_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }


@frappe.whitelist()
def setup_pos_shift_perms():
    """Grant the Cashier role the DocPerms needed to run shifts under Frappe security.

    Idempotent — safe to run repeatedly. Required after dropping ignore_permissions
    from pos_open_shift / pos_close_shift.
    """
    from frappe.permissions import add_permission, update_permission_property

    role = "Cashier"
    if not frappe.db.exists("Role", role):
        frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
            ignore_permissions=True
        )

    grants = {
        "POS Opening Entry": ["read", "write", "create", "submit"],
        "POS Closing Entry": ["read", "write", "create", "submit"],
        "POS Invoice": ["read", "write", "create", "submit"],
        "Sales Invoice": ["read", "write", "create", "submit"],
    }
    applied = {}
    for doctype, perms in grants.items():
        add_permission(doctype, role, 0)  # ensures a perm row at level 0 (sets read)
        for p in perms:
            update_permission_property(doctype, role, 0, p, 1)
        applied[doctype] = perms

    frappe.clear_cache()
    return {"role": role, "granted": applied}


@frappe.whitelist()
def setup_pos_order_invoice_link():
    """Point POS Order.pos_invoice at the POS Invoice doctype and add the reverse
    POS Invoice -> POS Order link field. Idempotent; avoids a full bench migrate.
    """
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    # POS Order.pos_invoice now links to POS Invoice (runtime override of field options)
    frappe.make_property_setter({
        "doctype": "POS Order",
        "fieldname": "pos_invoice",
        "property": "options",
        "value": "POS Invoice",
        "property_type": "Text",
    })

    # Reverse link on POS Invoice so the order shows on the invoice dashboard
    created_field = False
    if not frappe.db.has_column("POS Invoice", "pos_order"):
        create_custom_field("POS Invoice", {
            "fieldname": "pos_order",
            "label": "POS Order",
            "fieldtype": "Link",
            "options": "POS Order",
            "read_only": 1,
            "insert_after": "remarks",
        })
        created_field = True

    frappe.clear_cache()
    return {"pos_order.pos_invoice_options": "POS Invoice", "pos_invoice.pos_order_field_created": created_field}


@frappe.whitelist()
def setup_takeaway_field():
    """Add a per-line `takeaway` Check to POS Order Item (for mixed dine-in + take-away
    orders, where take-away items carry no service charge). Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "takeaway"):
        create_custom_field("POS Order Item", {
            "fieldname": "takeaway",
            "label": "Take Away",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "rate",
            "in_list_view": 1,
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.takeaway_field_created": created}


@frappe.whitelist()
def setup_invoice_takeaway_field():
    """Add a per-line `takeaway` Check to POS Invoice Item so the printed receipt can show
    take-away items separately (carried from the POS Order). Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Invoice Item", "takeaway"):
        create_custom_field("POS Invoice Item", {
            "fieldname": "takeaway",
            "label": "Take Away",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "rate",
            "in_list_view": 1,
        })
        created = True
    frappe.clear_cache()
    return {"pos_invoice_item.takeaway_field_created": created}


@frappe.whitelist()
def setup_split_fields():
    """Add a per-line `paid_qty` Int to POS Order Item (for bill splitting — how many of the
    line's qty have already been billed via a split). Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "paid_qty"):
        create_custom_field("POS Order Item", {
            "fieldname": "paid_qty",
            "label": "Paid Qty",
            "fieldtype": "Int",
            "default": "0",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.paid_qty_field_created": created}


@frappe.whitelist()
def setup_sent_qty_field():
    """Add a per-line `sent_qty` Int to POS Order Item — how many of the line's qty have
    already been fired to the kitchen (printed on a KOT). Lets a re-print show only the newly
    added items. Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        create_custom_field("POS Order Item", {
            "fieldname": "sent_qty",
            "label": "Sent Qty",
            "fieldtype": "Int",
            "default": "0",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.sent_qty_field_created": created}


@frappe.whitelist(methods=["POST"])
def mark_kot_printed():
    """Flag that the current items have been fired to the kitchen — sets each line's
    sent_qty to its qty, so the next KOT shows only items added afterwards."""
    _require_pos_role()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        setup_sent_qty_field()
    doc = frappe.get_doc("POS Order", order_name)
    for i in (doc.get("items") or []):
        if int(i.get("sent_qty") or 0) != int(i.qty or 0):
            frappe.db.set_value("POS Order Item", i.name, "sent_qty", int(i.qty or 0))
    # Firing the KOT promotes a draft to the kitchen queue.
    if doc.kitchen_status == "Draft":
        frappe.db.set_value("POS Order", order_name, "kitchen_status", "Pending")
    return {"ok": True}


@frappe.whitelist()
def setup_kitchen_status_options():
    """Allow 'Draft' (auto-created, not yet fired) and 'Cancelled' as kitchen_status values via a
    Property Setter, so insert()/save() don't reject them. Idempotent; no full migrate."""
    options = "Pending\nProcessing\nReady\nServed\nDraft\nCancelled"
    frappe.make_property_setter({
        "doctype": "POS Order", "fieldname": "kitchen_status",
        "property": "options", "value": options, "property_type": "Text",
    }, validate_fields_for_doctype=False)
    frappe.clear_cache(doctype="POS Order")
    return {"ok": True, "options": options}


@frappe.whitelist(methods=["POST"])
def delete_draft_order():
    """Delete an auto-created ticket that ended up empty — only when nothing has been fired to
    the kitchen and no invoice exists. Used for cleanup when the cart is cleared."""
    _require_pos_role()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name or not frappe.db.exists("POS Order", order_name):
        return {"ok": True, "deleted": False}
    doc = frappe.get_doc("POS Order", order_name)
    fired = sum(int(i.get("sent_qty") or 0) for i in (doc.get("items") or []))
    if doc.pos_invoice or fired > 0:
        return {"ok": True, "deleted": False}
    frappe.delete_doc("POS Order", order_name, ignore_permissions=True, force=True)
    return {"ok": True, "deleted": True}

def _order_fully_paid(pos_order):
    """True when every order line has been fully billed (via splits or a full pay)."""
    items = pos_order.get("items") or []
    if not items:
        return False
    return all((int(i.get("paid_qty") or 0) >= int(i.qty or 0)) for i in items)

# ─── Order Items Detail ──────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_order_items(order_name):
    if not order_name:
        return {"items": []}
    doc = frappe.get_doc("POS Order", order_name)
    items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate,
              "takeaway": int(i.get("takeaway") or 0), "paid_qty": int(i.get("paid_qty") or 0),
              "sent_qty": int(i.get("sent_qty") or 0),
              "remaining": int(i.qty or 0) - int(i.get("paid_qty") or 0)} for i in (doc.get("items") or [])]
    return {"items": items}

# ─── Orders by Table ─────────────────────────────────────

@frappe.whitelist()
def get_table_orders():
    table = frappe.local.form_dict.get("table", "")
    if not table:
        return {"orders": []}
    orders = frappe.get_all("POS Order",
        filters={"docstatus": 0, "restaurant_table": table},
        fields=["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "notes", "kitchen_status",
                "pos_invoice", "discount_amount", "discount_note"],
        order_by="creation asc"
    )
    # Exclude paid orders (have invoice)
    orders = [o for o in orders if not o.pos_invoice]
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        doc = frappe.get_doc("POS Order", o.name)
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate,
                  "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0)}
                 for i in (doc.get("items") or [])]
        o["items"] = items
    return {"orders": orders}


@frappe.whitelist()
def compare_orders():
    """Return details for two orders side by side (cancelled + re-created)."""
    old_name = frappe.local.form_dict.get("old", "")
    new_name = frappe.local.form_dict.get("new", "")
    result = {}
    for key, order_name in [("old", old_name), ("new", new_name)]:
        if not order_name:
            result[key] = None
            continue
        doc = frappe.get_doc("POS Order", order_name) if frappe.db.exists("POS Order", order_name) else None
        if not doc:
            result[key] = None
            continue
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate}
                 for i in (doc.get("items") or [])]
        result[key] = {
            "name": doc.name,
            "customer_name": doc.customer_name,
            "waiter_name": doc.waiter_name,
            "mobile": doc.mobile,
            "restaurant_table": doc.restaurant_table,
            "grand_total": doc.grand_total,
            "kitchen_status": doc.kitchen_status,
            "order_type": doc.order_type,
            "creation": str(doc.creation),
            "items": items,
            "items_count": len(items),
        }
    return result


# ─── Kitchen Display ─────────────────────────────────────
@frappe.whitelist()
def get_kitchen_orders():
    orders = frappe.get_all("POS Order",
        filters={"kitchen_status": ["not in", ["Served", "Draft", "Cancelled"]]},
        fields=["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "notes", "kitchen_status"],
        order_by="creation asc"
    )
    now_dt = now_datetime()
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        elapsed = (now_dt - o["creation"]).total_seconds()
        o["minutes"] = max(0, int(elapsed / 60))
        o["seconds"] = max(0, int(elapsed))
        doc = frappe.get_doc("POS Order", o.name)
        items = []
        for i in (doc.get("items") or []):
            item_doc = frappe.get_doc("Item", i.item)
            item_group = item_doc.item_group or ""
            kitchen_group = item_doc.get("kitchen_group") or ""
            kg_name = ""
            if kitchen_group:
                kg_name = frappe.db.get_value("Kitchen Group", kitchen_group, "group_name") or ""
            items.append({
                "item": i.item, "item_name": i.item_name or i.item,
                "qty": i.qty, "rate": i.rate,
                "group": item_group, "kitchen_group": kg_name
            })
        o["items_json"] = json.dumps(items)
    return {"orders": orders}

@frappe.whitelist(methods=["POST"])
def mark_kitchen_processing():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Processing")
    return {"status": "processing", "name": order_name}

@frappe.whitelist(methods=["POST"])
def mark_kitchen_ready():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Ready")
    return {"status": "ready", "name": order_name}

@frappe.whitelist(methods=["POST"])
def mark_kitchen_served():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order already served"))

    if pos_order.pos_invoice:
        try:
            inv = _get_invoice_doc(pos_order.pos_invoice)
            if inv.docstatus == 0:
                inv.flags.ignore_permissions = True
                inv.submit()
        except Exception:
            pass

    pos_order.db_set("docstatus", 1)
    pos_order.db_set("kitchen_status", "Served")
    return {"status": "served", "name": order_name}

# ─── Edit Order ─────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def update_order():
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw
    customer_name = data.get("customer_name", "").strip()
    mobile = data.get("mobile", "").strip()
    notes = data.get("notes", "").strip()

    if not order_name:
        frappe.throw(_("Order name is required"))
    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only draft orders can be edited"))

    # Track whether a receipt was already printed (invoice exists)
    was_printed = bool(pos_order.pos_invoice)

    order_type = pos_order.order_type or ""
    # Preserve per-line "already fired to kitchen" qty across the full items rebuild,
    # so a re-print of the KOT shows only the newly added items.
    # Keyed by (item, takeaway) so a same-item dine-in/takeaway split keeps each line's fired qty.
    sent_map = {}
    for i in (pos_order.get("items") or []):
        sent_map[(i.item, int(i.get("takeaway") or 0))] = int(i.get("sent_qty") or 0)
    # Update items
    pos_order.items = []
    subtotal = 0
    sc_base = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate,
                                   "takeaway": 1 if item_ta else 0,
                                   "sent_qty": min(sent_map.get((item_code, 1 if item_ta else 0), 0), qty)})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    if customer_name:
        pos_order.customer_name = customer_name
    if mobile:
        pos_order.mobile = mobile
    if notes:
        pos_order.notes = notes

    pos_order.flags.ignore_permissions = True
    pos_order.save()
    _apply_takeaway_service_charge(pos_order)

    # Also sync the linked POS Invoice if it exists
    if pos_order.pos_invoice:
        invoice = _get_invoice_doc(pos_order.pos_invoice)
        if invoice.docstatus == 0:
            invoice.items = []
            for item_data in items:
                item_code = item_data.get("item")
                qty = max(int(item_data.get("qty", 1)), 1)
                rate = float(item_data.get("rate", 0))
                if not rate:
                    rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
                invoice.append("items", {"item_code": item_code, "qty": qty, "rate": rate, "takeaway": 1 if _item_is_takeaway(item_data, order_type) else 0})

            invoice.taxes = []
            if sc_amount > 0:
                _append_service_charge_tax(invoice, sc_amount)

            invoice.payments = []
            existing_payment_mode = ""
            old_inv = _get_invoice_doc(pos_order.pos_invoice)
            if old_inv.payments:
                existing_payment_mode = old_inv.payments[0].mode_of_payment
            invoice.append("payments", {"mode_of_payment": existing_payment_mode or "Cash", "amount": grand_total})

            invoice.flags.ignore_permissions = True
            invoice.save()

    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "invoice_name": pos_order.pos_invoice or "",
        "was_printed": was_printed,
    }


@frappe.whitelist(allow_guest=True)
def get_order_changelog(order_name):
    if not order_name:
        return {"changelog": []}

    # Fetch versions (Frappe's built-in version tracking)
    versions = frappe.get_all("Version",
        filters={"ref_doctype": "POS Order", "docname": order_name},
        fields=["name", "creation", "owner", "data"],
        order_by="creation asc"
    )

    changelog = []
    for v in versions:
        entry = {"version": v.name, "created": str(v.creation), "owner": v.owner, "changes": []}
        if v.data:
            try:
                data = frappe.parse_json(v.data)
                changed = data.get("changed") or []
                for field, old, new in changed:
                    entry["changes"].append({
                        "field": field,
                        "old_value": old,
                        "new_value": new,
                    })
                # Also check for added/removed table rows
                added = data.get("added") or []
                removed = data.get("removed") or []
                row_changes = data.get("row_changed") or []
                if added:
                    entry["changes"].append({"field": "items", "action": "added", "count": len(added)})
                if removed:
                    entry["changes"].append({"field": "items", "action": "removed", "count": len(removed)})
                if row_changes:
                    entry["changes"].append({"field": "items", "action": "modified", "count": len(row_changes)})
            except Exception:
                pass
        changelog.append(entry)

    return {"changelog": changelog, "order_name": order_name}

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

    # Validate table exists — if not, silently ignore it
    if table:
        table_exists = frappe.db.get_value("Restaurant Table", table, "name")
        if not table_exists:
            table = ""

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
        "kitchen_status": "Pending",
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
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    _apply_takeaway_service_charge(pos_order)
    frappe.db.commit()

    # Process cashback loyalty for this order
    if mobile and pos_order.grand_total > 0:
        try:
            _process_cashback(mobile, pos_order.name, pos_order.grand_total)
        except Exception as e:
            frappe.log_error(f"Cashback error in kiosk: {str(e)}", "Cashback")

    # Send WhatsApp to restaurant + customer
    try:
        send_order_whatsapp(pos_order.name)
    except Exception as e:
        frappe.log_error(f"Restaurant WhatsApp error: {str(e)}", "WhatsApp")
    if mobile:
        try:
            send_customer_whatsapp(pos_order.name, mobile)
        except Exception as e:
            frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")

    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "table": table,
        "customer_name": customer_name,
        "mobile": mobile,
    }

# ─── Kiosk Setup ─────────────────────────────────────────

@frappe.whitelist()
def setup_kiosk_tools():
    """One-time setup: create Item Review DocType + custom fields on Item."""
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


# ─── Item Info (for detail overlay) ──────────────────────

@frappe.whitelist(allow_guest=True)
def get_item_info(item_code):
    if not item_code:
        return {}
    doc = frappe.get_doc("Item", item_code)
    return {
        "item_name": doc.item_name,
        "description": doc.description or "",
        "image": doc.image or "",
        "standard_rate": doc.standard_rate or 0,
        "speciality_tags": doc.get("speciality_tags") or "",
        "youtube_url": doc.get("youtube_url") or "",
        "prep_time": doc.get("prep_time") or 0,
        "item_group": doc.item_group or "",
    }

# ─── Item Reviews ────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_item_reviews(item_code):
    if not item_code:
        return {"reviews": []}
    reviews = frappe.get_all("Item Review",
        filters={"item": item_code},
        fields=["customer_name", "rating", "comment", "creation"],
        order_by="creation desc",
        limit=20
    )
    avg_rating = 0
    if reviews:
        avg_rating = sum(r["rating"] or 0 for r in reviews) / len(reviews)
    return {"reviews": reviews, "avg_rating": round(avg_rating, 1), "count": len(reviews)}


@frappe.whitelist(allow_guest=True)
def submit_item_review():
    data = frappe.local.form_dict
    item = data.get("item", "").strip()
    customer_name = data.get("customer_name", "").strip()
    rating = int(data.get("rating", 0))
    comment = data.get("comment", "").strip()
    submitted_by = data.get("submitted_by", "").strip()

    if not item or not customer_name or not rating:
        return {"success": False, "error": "Item, name and rating required"}

    review = frappe.get_doc({
        "doctype": "Item Review",
        "item": item,
        "customer_name": customer_name,
        "rating": min(max(rating, 1), 5),
        "comment": comment,
        "submitted_by": submitted_by,
    })
    review.flags.ignore_permissions = True
    review.insert()
    return {"success": True, "name": review.name}

# ─── Loyalty Programme Setup ──────────────────────────────

@frappe.whitelist()
def setup_loyalty():
    """Create loyalty DocTypes and Settings."""
    created = []

    if not frappe.db.exists("DocType", "Loyalty Customer"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Loyalty Customer", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "referral_code", "label": "Referral Code", "fieldtype": "Data", "unique": 1},
                {"fieldname": "referred_by", "label": "Referred By", "fieldtype": "Link", "options": "Loyalty Customer"},
                {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_redeemed", "label": "Total Redeemed", "fieldtype": "Currency", "default": 0},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Loyalty Customer")

    if not frappe.db.exists("DocType", "Cashback Transaction"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Cashback Transaction", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "loyalty_customer", "label": "Loyalty Customer", "fieldtype": "Link", "options": "Loyalty Customer", "reqd": 1, "in_list_view": 1},
                {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nRedeem\nReferral Discount", "reqd": 1, "in_list_view": 1},
                {"fieldname": "level", "label": "Level", "fieldtype": "Int"},
                {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1},
                {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
                {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Cashback Transaction")

    if not frappe.db.exists("DocType", "Loyalty Settings"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Loyalty Settings", "module": "Zeloura", "custom": 1, "issingle": 1,
            "fields": [
                {"fieldname": "level_1_percent", "label": "Level 1 (%)", "fieldtype": "Percent", "default": 5.0},
                {"fieldname": "level_2_percent", "label": "Level 2 (%)", "fieldtype": "Percent", "default": 2.0},
                {"fieldname": "level_3_percent", "label": "Level 3 (%)", "fieldtype": "Percent", "default": 1.0},
                {"fieldname": "min_redeem", "label": "Minimum Redeem (LKR)", "fieldtype": "Currency", "default": 200},
                {"fieldname": "referral_discount_percent", "label": "Referral Discount (%)", "fieldtype": "Percent", "default": 5.0},
                {"fieldname": "expiry_days", "label": "Expiry Days", "fieldtype": "Int", "default": 365},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Loyalty Settings")

    # Ensure default settings record exists
    if not frappe.db.exists("Loyalty Settings", "Loyalty Settings"):
        s = frappe.get_doc({"doctype": "Loyalty Settings"})
        s.flags.ignore_permissions = True
        s.insert()

    return {"status": "created" if created else "already_exists", "created": created}


# ─── Loyalty Helpers ─────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def debug_loyalty():
    mobile = frappe.local.form_dict.get("mobile", "")
    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"error": "No loyalty customer"}
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    ref_by_name = ""
    if lc.get("referred_by"):
        ref_doc = frappe.get_doc("Loyalty Customer", lc.get("referred_by"))
        ref_by_name = ref_doc.mobile
    return {"mobile": lc.mobile, "code": lc.referral_code, "referred_by": ref_by_name, "balance": lc.cashback_balance}

def _get_loyalty_customer(mobile):
    """Get or create a Loyalty Customer record for this mobile."""
    existing = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if existing:
        return existing
    # Create new Loyalty Customer
    code = _generate_referral_code()
    lc = frappe.get_doc({
        "doctype": "Loyalty Customer",
        "customer_name": "",
        "mobile": mobile,
        "referral_code": code,
        "cashback_balance": 0,
        "total_earned": 0,
        "total_redeemed": 0,
    })
    lc.flags.ignore_permissions = True
    lc.insert()
    frappe.db.commit()
    return lc.name


def _generate_referral_code():
    """Generate a unique 6-char referral code like LUUV7X."""
    for _ in range(50):
        code = "LV" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if not frappe.db.exists("Loyalty Customer", {"referral_code": code}):
            return code
    return "LV" + secrets.token_hex(3).upper()[:4]


def _get_loyalty_settings():
    """Get loyalty settings dict."""
    if frappe.db.exists("Loyalty Settings", "Loyalty Settings"):
        doc = frappe.get_doc("Loyalty Settings", "Loyalty Settings")
        return {
            "level_1_percent": float(doc.level_1_percent or 5.0),
            "level_2_percent": float(doc.level_2_percent or 2.0),
            "level_3_percent": float(doc.level_3_percent or 1.0),
            "min_redeem": float(doc.min_redeem or 200),
            "referral_discount_percent": float(doc.referral_discount_percent or 5.0),
        }
    return {"level_1_percent": 5.0, "level_2_percent": 2.0, "level_3_percent": 1.0, "min_redeem": 200, "referral_discount_percent": 5.0}


def _credit_cashback(lc_name, amount, level, ref_order="", notes=""):
    """Credit cashback to a Loyalty Customer and log transaction."""
    if not lc_name or amount <= 0:
        return
    # Credit balance
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    lc.cashback_balance = (lc.cashback_balance or 0) + amount
    lc.total_earned = (lc.total_earned or 0) + amount
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)

    # Log transaction
    txn = frappe.get_doc({
        "doctype": "Cashback Transaction",
        "loyalty_customer": lc_name,
        "type": "Earn",
        "level": level,
        "amount": amount,
        "reference_order": ref_order,
        "notes": notes,
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    frappe.db.commit()


def _process_cashback(mobile, order_name, grand_total):
    """After an order, credit cashback up to 3 levels up the referral chain."""
    try:
        lc_name = _get_loyalty_customer(mobile)
        lc = frappe.get_doc("Loyalty Customer", lc_name)
        settings = _get_loyalty_settings()

        # Simple approach: get the referred_by chain directly
        current_ref = lc.get("referred_by")

        for level_idx, percent_key in enumerate(["level_1_percent", "level_2_percent", "level_3_percent"]):
            if current_ref:
                ref_lc = frappe.get_doc("Loyalty Customer", current_ref)
                amount = round(grand_total * settings[percent_key] / 100, 2)
                if amount > 0:
                    _credit_cashback(ref_lc.name, amount, level_idx + 1, order_name,
                                     f"Level {level_idx+1} cashback from order {order_name} by {mobile}")
                current_ref = ref_lc.get("referred_by")
            else:
                break
    except Exception as e:
        frappe.log_error(f"Cashback error: {str(e)}", "Cashback")


# ─── Loyalty API ─────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def loyalty_get_info():
    mobile = frappe.local.form_dict.get("mobile", "").strip()
    if not mobile:
        return {"success": False, "error": "Mobile required"}

    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Not found", "has_account": False}

    lc = frappe.get_doc("Loyalty Customer", lc_name)
    settings = _get_loyalty_settings()

    # Get direct referrals (Level 1 downline)
    downline = frappe.get_all("Loyalty Customer", filters={"referred_by": lc_name},
                              fields=["customer_name", "mobile", "total_earned", "creation"])
    downline_data = [{"name": d.customer_name or d.mobile, "mobile": d.mobile, "total_earned": d.total_earned or 0}
                     for d in downline]

    return {
        "success": True,
        "customer_name": lc.customer_name or "",
        "mobile": lc.mobile,
        "referral_code": lc.referral_code or "",
        "cashback_balance": lc.cashback_balance or 0,
        "total_earned": lc.total_earned or 0,
        "total_redeemed": lc.total_redeemed or 0,
        "downline_count": len(downline_data),
        "downline": downline_data,
        "settings": settings,
    }


@frappe.whitelist(allow_guest=True)
def loyalty_generate_code():
    mobile = frappe.local.form_dict.get("mobile", "").strip()
    if not mobile:
        return {"success": False, "error": "Mobile required"}

    lc_name = _get_loyalty_customer(mobile)
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    if lc.referral_code:
        return {"success": True, "referral_code": lc.referral_code}

    lc.referral_code = _generate_referral_code()
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)
    return {"success": True, "referral_code": lc.referral_code}


@frappe.whitelist(allow_guest=True)
def loyalty_apply_referral():
    mobile = frappe.local.form_dict.get("mobile", "").strip()
    ref_code = frappe.local.form_dict.get("ref_code", "").strip().upper()

    if not mobile or not ref_code:
        return {"success": False, "error": "Mobile and referral code required"}

    # Find referrer by code
    referrer = frappe.db.get_value("Loyalty Customer", {"referral_code": ref_code}, "name")
    if not referrer:
        return {"success": False, "error": "Invalid referral code"}

    ref_doc = frappe.get_doc("Loyalty Customer", referrer)
    if ref_doc.mobile == mobile:
        return {"success": False, "error": "Cannot refer yourself"}

    # Get or create customer
    lc_name = _get_loyalty_customer(mobile)
    lc = frappe.get_doc("Loyalty Customer", lc_name)

    if lc.referred_by:
        return {"success": False, "error": "Already linked to a referrer"}

    lc.referred_by = referrer
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)
    frappe.db.commit()

    return {"success": True, "referrer": ref_doc.customer_name or ref_doc.mobile}


@frappe.whitelist(allow_guest=True)
def loyalty_redeem():
    mobile = frappe.local.form_dict.get("mobile", "").strip()
    amount = float(frappe.local.form_dict.get("amount", 0))

    if not mobile or amount <= 0:
        return {"success": False, "error": "Valid mobile and amount required"}

    lc_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
    if not lc_name:
        return {"success": False, "error": "Account not found"}

    lc = frappe.get_doc("Loyalty Customer", lc_name)
    settings = _get_loyalty_settings()

    if amount < settings["min_redeem"]:
        return {"success": False, "error": f"Minimum redeem is LKR {settings['min_redeem']:.0f}"}

    if (lc.cashback_balance or 0) < amount:
        return {"success": False, "error": f"Insufficient balance. Available: LKR {lc.cashback_balance:.0f}"}

    lc.cashback_balance = (lc.cashback_balance or 0) - amount
    lc.total_redeemed = (lc.total_redeemed or 0) + amount
    lc.flags.ignore_permissions = True
    lc.save(ignore_permissions=True)

    txn = frappe.get_doc({
        "doctype": "Cashback Transaction",
        "loyalty_customer": lc_name,
        "type": "Redeem",
        "amount": amount,
        "notes": f"Redeemed LKR {amount:.0f}",
    })
    txn.flags.ignore_permissions = True
    txn.insert()
    frappe.db.commit()

    return {"success": True, "new_balance": lc.cashback_balance}


@frappe.whitelist(allow_guest=True)
def loyalty_find_referrer():
    ref_code = frappe.local.form_dict.get("ref_code", "").strip().upper()
    if not ref_code:
        return {"found": False}
    lc_name = frappe.db.get_value("Loyalty Customer", {"referral_code": ref_code}, "name")
    if not lc_name:
        return {"found": False, "ref_code": ref_code}
    lc = frappe.get_doc("Loyalty Customer", lc_name)
    return {"found": True, "referrer_name": lc.customer_name or lc.mobile, "ref_code": ref_code}


@frappe.whitelist(allow_guest=True)
def get_kitchen_groups():
    groups = frappe.get_all("Kitchen Group",
        fields=["group_name", "display_order", "color"],
        order_by="display_order asc"
    )
    return {"groups": groups}


@frappe.whitelist(allow_guest=True)
def loyalty_get_settings_api():
    return _get_loyalty_settings()


# ─── Kitchen Group Setup ──────────────────────────────────

@frappe.whitelist()
def setup_kitchen_groups():
    """Create Kitchen Group DocType + assign items to groups."""
    created = []

    if not frappe.db.exists("DocType", "Kitchen Group"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Kitchen Group", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "group_name", "label": "Group Name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "display_order", "label": "Display Order", "fieldtype": "Int", "default": 0},
                {"fieldname": "color", "label": "Color", "fieldtype": "Data", "description": "Hex color e.g. #F59E0B"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Kitchen Group")

        # Create default groups
        default_groups = [
            ("Kottu", 1, "#F59E0B"),
            ("Rice & Noodles", 2, "#3B82F6"),
            ("Curries & Gravies", 3, "#10B981"),
            ("Biryani", 4, "#8B5CF6"),
            ("Pizza", 5, "#EC4899"),
            ("Beverages", 6, "#06B6D4"),
            ("Coffee & Tea", 7, "#F97316"),
            ("Desserts", 8, "#14B8A6"),
            ("Sides & Starters", 9, "#6366F1"),
            ("Soups & Salads", 10, "#84CC16"),
            ("Grills & Sizzlers", 11, "#EF4444"),
            ("Breads", 12, "#D946EF"),
            ("Other", 99, "#6B7280"),
        ]
        for name, order, color in default_groups:
            g = frappe.get_doc({
                "doctype": "Kitchen Group",
                "group_name": name,
                "display_order": order,
                "color": color,
            })
            g.flags.ignore_permissions = True
            g.insert()
        created.append(f"{len(default_groups)} default groups")

    # Add kitchen_group field to Item
    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "kitchen_group"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": "kitchen_group",
            "label": "Kitchen Group",
            "fieldtype": "Link",
            "options": "Kitchen Group",
            "insert_after": "item_group",
        })
        cf.insert()
        created.append("kitchen_group field on Item")

    return {"status": "created" if created else "already_exists", "created": created}


@frappe.whitelist()
def assign_kitchen_groups():
    """Assign items to kitchen groups based on their item_group."""
    mapping = {
        "Kottu": "Kottu", "Cheese Kottu": "Kottu", "Signature Kottu": "Kottu",
        "Fried Rice": "Rice & Noodles", "Rice & Curry": "Rice & Noodles",
        "Biryani & Naan Combos": "Biryani", "Biryani": "Biryani",
        "Pizza": "Pizza",
        "Grills": "Grills & Sizzlers", "Sizzler Platters": "Grills & Sizzlers",
        "North Asian": "Rice & Noodles", "Sri Lankan Fusion": "Rice & Noodles",
        "Indian Cuisine": "Curries & Gravies",
        "European Dishes & Salads": "Sides & Starters",
        "Soups": "Soups & Salads",
        "Sides & Accompaniments": "Sides & Starters",
        "Vegetarian": "Curries & Gravies",
        "Coffee & Tea": "Coffee & Tea",
        "Beverages": "Beverages",
        "Desserts": "Desserts",
    }
    items = frappe.get_all("Item", fields=["name", "item_group"])
    assigned = 0
    for item in items:
        grp = item.item_group or ""
        kg_name = mapping.get(grp, "Other")
        kg = frappe.db.get_value("Kitchen Group", {"group_name": kg_name}, "name")
        if kg:
            frappe.db.set_value("Item", item.name, "kitchen_group", kg)
            assigned += 1
    frappe.db.commit()
    return {"status": "done", "assigned": assigned}

# ─── Customer Account Setup ──────────────────────────────

@frappe.whitelist()
def setup_customer_accounts():
    """One-time setup: create Customer Account + Customer Token DocTypes."""
    created = []
    if frappe.db.exists("DocType", "Customer Account"):
        frappe.delete_doc("DocType", "Customer Account", force=1)
        frappe.db.commit()
    if not frappe.db.exists("DocType", "Customer Account"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Customer Account", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
                {"fieldname": "pin", "label": "PIN", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "last_login", "label": "Last Login", "fieldtype": "Datetime"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Customer Account")

    if frappe.db.exists("DocType", "Customer Token"):
        frappe.delete_doc("DocType", "Customer Token", force=1)
        frappe.db.commit()
    if not frappe.db.exists("DocType", "Customer Token"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Customer Token", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Customer Token")

    return {"status": "created" if created else "already_exists", "created": created}


# ─── Customer Account API ─────────────────────────────────

def _get_or_create_customer_token(mobile):
    # Clean existing expired tokens (older than 7 days)
    frappe.db.sql("DELETE FROM `tabCustomer Token` WHERE created < NOW() - INTERVAL 7 DAY")
    frappe.db.commit()
    # Check for existing valid token
    existing = frappe.db.get_value("Customer Token", {"mobile": mobile}, "token")
    if existing:
        return existing
    # Create new token
    token = secrets.token_hex(16)
    ct = frappe.get_doc({
        "doctype": "Customer Token",
        "token": token,
        "mobile": mobile,
        "created": now_datetime(),
    })
    ct.flags.ignore_permissions = True
    ct.insert()
    frappe.db.commit()
    return token


@frappe.whitelist(allow_guest=True)
def customer_create_or_login():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    name = data.get("name", "").strip()

    if not mobile or len(mobile) < 10:
        return {"success": False, "error": "Valid phone number required"}

    existing = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")

    if existing:
        # Return that account exists — PIN verification needed
        account = frappe.get_doc("Customer Account", existing)
        return {"success": True, "exists": True, "name": account.customer_name or ""}

    # New account — set PIN in next step
    frappe.db.set_value("Customer Account", None, {})  # dummy
    return {"success": True, "exists": False}


@frappe.whitelist(allow_guest=True)
def customer_set_pin():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    pin = data.get("pin", "").strip()
    name = data.get("name", "").strip()

    if not mobile or len(mobile) < 10:
        return {"success": False, "error": "Valid phone required"}
    if not pin or len(pin) != 4 or not pin.isdigit():
        return {"success": False, "error": "PIN must be 4 digits"}

    existing = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")
    if existing:
        return {"success": False, "error": "Account already exists"}

    account = frappe.get_doc({
        "doctype": "Customer Account",
        "mobile": mobile,
        "customer_name": name,
        "pin": pin,
        "last_login": now_datetime(),
    })
    account.flags.ignore_permissions = True
    account.insert(ignore_permissions=True)
    frappe.db.commit()

    token = _get_or_create_customer_token(mobile)
    return {"success": True, "token": token, "name": name}


@frappe.whitelist(allow_guest=True)
def customer_verify_pin():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    pin = data.get("pin", "").strip()

    if not mobile or not pin:
        return {"success": False, "error": "Phone and PIN required"}

    accounts = frappe.get_all("Customer Account", filters={"mobile": mobile}, fields=["name", "customer_name", "pin"])
    if not accounts:
        return {"success": False, "error": "Account not found"}

    account = accounts[0]
    # PIN is stored as password field — verify
    stored = frappe.db.get_value("Customer Account", account["name"], "pin")
    if not stored or stored != pin:
        return {"success": False, "error": "Wrong PIN"}

    # Update last login
    frappe.db.set_value("Customer Account", account["name"], "last_login", now_datetime())

    token = _get_or_create_customer_token(mobile)
    return {"success": True, "token": token, "name": account["customer_name"]}


@frappe.whitelist(allow_guest=True)
def customer_get_orders():
    data = frappe.local.form_dict
    token = data.get("token", "").strip()

    if not token:
        # Try direct mobile param (for backward compatibility)
        mobile = data.get("mobile", "").strip()
        if not mobile:
            return {"success": False, "error": "Login required"}
    else:
        # Validate token
        token_doc = frappe.db.get_value("Customer Token", {"token": token}, "mobile")
        if not token_doc:
            return {"success": False, "error": "Invalid or expired token"}
        mobile = token_doc

    # Fetch all orders for this mobile
    orders = frappe.get_all("POS Order",
        filters={"mobile": mobile},
        fields=["name", "customer_name", "restaurant_table", "grand_total",
                "kitchen_status", "docstatus", "creation", "modified", "order_source"],
        order_by="creation desc"
    )

    result = []
    for o in orders:
        doc = frappe.get_doc("POS Order", o.name)
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])]
        result.append({
            "name": o.name,
            "customer_name": o.customer_name,
            "table": o.restaurant_table or "",
            "grand_total": o.grand_total or 0,
            "status": o.kitchen_status or "Pending",
            "docstatus": o.docstatus,
            "placed_at": str(o.creation),
            "completed_at": str(o.modified) if o.docstatus == 1 else "",
            "time_ago": frappe.utils.pretty_date(o.creation),
            "items": items,
            "item_count": len(items),
        })

    return {"success": True, "orders": result, "mobile": mobile}

# ─── Print Logging ───────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def log_print():
    data = frappe.local.form_dict
    order_name = data.get("order_name", "")
    print_type = data.get("print_type", "")  # "receipt", "kot"
    user = frappe.session.user
    user_full = frappe.get_value("User", user, "full_name") or user

    log = frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Info",
        "reference_doctype": "POS Order",
        "reference_name": order_name,
        "content": f"{print_type.upper()} printed by {user_full} ({user})",
    })
    log.flags.ignore_permissions = True
    log.insert(ignore_permissions=True)

    # Also log to Frappe error log for audit
    frappe.log_error(
        f"PRINT: {print_type.upper()} | Order: {order_name} | By: {user_full} ({user})",
        "POS Print Log"
    )

    return {"status": "logged", "print_type": print_type, "user": user_full}

# ─── Offline Sync ─────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_menu_data():
    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    items = []
    tables = []
    restaurant = frappe.get_doc("Restaurant", restaurant_name) if restaurant_name else None
    if restaurant and restaurant.active_menu:
        menu = frappe.get_doc("Restaurant Menu", restaurant.active_menu)
        if menu.enabled:
            for row in menu.items:
                if not row.item:
                    continue
                doc = frappe.get_doc("Item", row.item)
                items.append({
                    "code": row.item,
                    "name": doc.item_name or row.item,
                    "description": doc.description or "",
                    "image": doc.image or "",
                    "rate": row.rate or 0,
                    "group": doc.item_group or "General",
                })
    tables = frappe.get_all("Restaurant Table", fields=["name"], order_by="name asc")
    return {"items": items, "tables": tables}


@frappe.whitelist(methods=["POST"])
def sync_offline_orders():
    orders_raw = frappe.local.form_dict.get("orders")
    orders = frappe.parse_json(orders_raw) if isinstance(orders_raw, str) else (orders_raw or [])
    results = []
    for o in orders:
        local_id = o.get("local_id", "")
        items = o.get("items", [])
        table = o.get("table", "")
        customer_name = o.get("customer_name", "").strip() or "Walk-in"
        mobile = o.get("mobile", "").strip() or ""
        notes = o.get("notes", "").strip()
        payment_mode = o.get("payment_mode", "Cash")
        cash_amount = float(o.get("cash_amount", 0))
        card_amount = float(o.get("card_amount", 0))
        order_type = o.get("order_type", "Dine In")
        pos_profile_name = _active_pos_profile_name(o.get("pos_profile", ""))

        if not items:
            results.append({"local_id": local_id, "status": "failed", "error": "No items"})
            continue

        try:
            rate_map = {}
            for item_data in items:
                code = item_data.get("item") or item_data.get("code")
                qty = max(int(item_data.get("qty", 1)), 1)
                rate = float(item_data.get("rate", 0))
                if not rate:
                    rate = _resolve_item_rate(code, 0, _get_pos_price_list())
                rate_map[code] = {"qty": qty, "rate": rate,
                                  "takeaway": 1 if _item_is_takeaway(item_data, order_type) else 0}

            invoice_items = []
            subtotal = 0
            sc_base = 0
            for code, ri in rate_map.items():
                invoice_items.append({"item_code": code, "qty": ri["qty"], "rate": ri["rate"], "takeaway": ri["takeaway"]})
                subtotal += ri["rate"] * ri["qty"]
                if not ri["takeaway"]:
                    sc_base += ri["rate"] * ri["qty"]

            sc_rate, sc_amount = _service_charge_on(sc_base)
            grand_total = subtotal + sc_amount

            payments = [{"mode_of_payment": payment_mode, "amount": grand_total}]
            if payment_mode == "Cash+Card":
                payments = []
                if cash_amount > 0:
                    payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
                if card_amount > 0:
                    payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})

            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
            _ensure_open_shift(pos_profile_name)

            invoice = frappe.get_doc({
                "doctype": _invoice_doctype_for(pos_profile_name),
                "is_pos": 1,
                "pos_profile": pos_profile_name,
                "customer": "Walk In",
                "company": pos_profile.company,
                "currency": pos_profile.currency or "LKR",
                "selling_price_list": pos_profile.selling_price_list or "",
                "set_warehouse": pos_profile.warehouse or "",
                "update_stock": 0,
                "posting_date": now_datetime().strftime("%Y-%m-%d"),
                "remarks": f"Offline Synced | {customer_name} | Table: {table}",
                "items": [],
                "payments": payments,
            })
            for inv_item in invoice_items:
                invoice.append("items", inv_item)

            if sc_amount > 0:
                _append_service_charge_tax(invoice, sc_amount)

            invoice.flags.ignore_permissions = True
            invoice.insert()

            waiter_name = customer_name
            pos_order = frappe.get_doc({
                "doctype": "POS Order",
                "naming_series": "POS-",
                "customer_name": customer_name,
                "waiter_name": waiter_name,
                "mobile": mobile,
                "restaurant_table": table,
                "order_source": "Walk-in",
                "order_type": order_type,
                "kitchen_status": "Pending",
                "grand_total": grand_total,
                "service_charge_rate": sc_rate,
                "service_charge_amount": sc_amount,
                "pos_invoice": invoice.name,
                "notes": notes,
                "items": [],
            })
            for code, ri in rate_map.items():
                item_name = frappe.db.get_value("Item", code, "item_name") or code
                pos_order.append("items", {"item": code, "item_name": item_name, "qty": ri["qty"], "rate": ri["rate"], "takeaway": ri["takeaway"]})
            pos_order.flags.ignore_permissions = True
            pos_order.flags.ignore_links = True
            pos_order.insert()
            _apply_takeaway_service_charge(pos_order)

            _link_invoice_to_order(invoice.name, pos_order.name)

            results.append({
                "local_id": local_id,
                "status": "synced",
                "server_name": pos_order.name,
                "invoice_name": invoice.name,
                "grand_total": grand_total,
                "service_charge_rate": sc_rate,
                "service_charge_amount": sc_amount,
            })
        except Exception as e:
            frappe.log_error(f"Offline sync error for {local_id}: {str(e)}", "OfflineSync")
            results.append({"local_id": local_id, "status": "failed", "error": str(e)})

    return {"results": results}

# ─── Print Data ─────────────────────────────────────────

@frappe.whitelist()
def get_receipt_print_data(order_name):
    """Return data for printing a POS receipt."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": i.qty,
            "rate": i.rate,
        })

    invoice_name = doc.pos_invoice or ""
    payment_mode = ""
    if invoice_name:
        inv = _get_invoice_doc(invoice_name)
        if inv.payments:
            payment_mode = inv.payments[0].mode_of_payment

    return {
        "order_name": doc.name,
        "invoice_name": invoice_name,
        "customer_name": doc.customer_name or "Walk-in",
        "table": doc.restaurant_table or "",
        "waiter_name": doc.waiter_name or "",
        "grand_total": doc.grand_total or 0,
        "service_charge_rate": doc.service_charge_rate or 0,
        "service_charge_amount": doc.service_charge_amount or 0,
        "payment_mode": payment_mode,
        "items": items,
        "creation": str(doc.creation),
    }


@frappe.whitelist()
def get_guest_check_data(order_name):
    """Return data for printing a Guest Check (pre-bill)."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": i.qty,
            "rate": i.rate,
        })

    return {
        "order_name": doc.name,
        "customer_name": doc.customer_name or "Walk-in",
        "table": doc.restaurant_table or "",
        "waiter_name": doc.waiter_name or "",
        "order_type": doc.order_type or "Dine In",
        "grand_total": doc.grand_total or 0,
        "service_charge_rate": doc.service_charge_rate or 0,
        "service_charge_amount": doc.service_charge_amount or 0,
        "items": items,
        "creation": str(doc.creation),
    }


@frappe.whitelist()
def get_kot_print_data(order_name):
    """Return data for printing a Kitchen Order Ticket."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        # Only items not yet fired to the kitchen (a re-print shows only newly added items).
        new_qty = int(i.qty or 0) - int(i.get("sent_qty") or 0)
        if new_qty <= 0:
            continue
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": new_qty,
            "takeaway": int(i.get("takeaway") or 0),
        })

    return {
        "order_name": doc.name,
        "table": doc.restaurant_table or "",
        "order_type": doc.get("order_type") or "",
        "waiter_name": doc.waiter_name or "",
        "order_source": doc.order_source or "Walk-in",
        "notes": doc.notes or "",
        "items": items,
        "creation": str(doc.creation),
    }


@frappe.whitelist()
def get_pos_print_formats():
    """Print Formats available for POS printing, grouped by target doctype.
    'invoice' formats render the receipt (POS Invoice); 'order' formats render
    the guest check and KOT (POS Order)."""
    def _fmts(dt):
        return frappe.get_all(
            "Print Format",
            filters={"doc_type": dt, "disabled": 0},
            fields=["name"], order_by="name",
        )
    return {
        "invoice": _fmts("POS Invoice"),   # receipt
        "order": _fmts("POS Order"),       # guest check + KOT
    }


@frappe.whitelist()
def render_pos_print(order_name, kind="receipt", print_format=None):
    """Render a POS document with a selected Frappe Print Format.
    kind: 'receipt' -> POS Invoice; 'guest_check'/'kot' -> POS Order.
    Returns {"html": ""} when no format is chosen (or the order is not yet
    invoiced for a receipt), so the caller falls back to the built-in layout."""
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not print_format:
        return {"html": ""}
    if kind == "receipt":
        order = frappe.get_doc("POS Order", order_name)
        if not order.pos_invoice:
            return {"html": ""}   # not paid yet → fall back to built-in
        dt = "POS Invoice" if frappe.db.exists("POS Invoice", order.pos_invoice) else "Sales Invoice"
        dn = order.pos_invoice
    else:
        dt, dn = "POS Order", order_name
    html = frappe.get_print(dt, dn, print_format=print_format)
    return {"html": html}


# ─── WhatsApp Order Confirmation ─────────────────────────

def send_order_whatsapp(order_name):
    """Send order details to the restaurant's WhatsApp business number."""
    order = frappe.get_doc("POS Order", order_name)

    if not frappe.db.exists("WhatsApp Account", {}):
        return

    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return

    acc = frappe.get_doc("WhatsApp Account", wa_account)

    items_text = ""
    for i in (order.get("items") or []):
        items_text += f"- {i.item_name or i.item} x{i.qty} = LKR {i.rate * i.qty:.0f}\n"

    message = (
        f"*New Order - {order.name}*\n"
        f"👤 {order.customer_name or 'Online Guest'}\n"
        f"🪑 {order.restaurant_table or 'N/A'}\n"
        f"📱 {order.mobile or 'N/A'}\n"
        f"📝 {order.notes or ''}\n\n"
        f"*Items:*\n{items_text}\n"
        f"*Total:* LKR {order.grand_total:.0f}\n\n"
        f"Reply *OK* to accept this order."
    )

    # Send to restaurant monitoring number
    to_number = "94773429923"

    try:
        import requests
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": message, "preview_url": False},
        }
        resp = requests.post(url, headers=headers, json=data)
        result = resp.json()
        if resp.status_code == 200 and "messages" in result:
            msg_id = result["messages"][0]["id"]
            frappe.db.set_value("POS Order", order.name, "wa_message_id", msg_id)
            frappe.db.set_value("POS Order", order.name, "whatsapp_status", "Sent")
            frappe.db.commit()
            frappe.log_error(f"Order notification sent for {order.name}", "WhatsApp")
        else:
            frappe.log_error(f"Order notification failed for {order.name}: {resp.text}", "WhatsApp")
    except Exception as e:
        frappe.log_error(f"WhatsApp error for {order.name}: {str(e)}", "WhatsApp")


def send_customer_whatsapp(order_name, mobile):
    """Send tracking link to customer via WhatsApp."""
    if not mobile or not frappe.db.exists("WhatsApp Account", {}):
        return
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return

    track_url = f"https://luuvgrand.com/app?phone={mobile}"
    message = (
        f"*Order #{order_name} - Luuv Fryxo* 🎉\n\n"
        f"Thank you for your order!\n\n"
        f"View your orders: {track_url}\n\n"
        f"Your order is pending confirmation. Please wait for the restaurant to accept it."
    )

    try:
        import requests
        acc = frappe.get_doc("WhatsApp Account", wa_account)
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": mobile, "type": "text", "text": {"body": message, "preview_url": True}}
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            frappe.log_error(f"Tracking link sent to {mobile} for {order_name}", "WhatsApp")
    except Exception as e:
        frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")


def process_wa_reply(doc, method):
    """Auto-reply to incoming WhatsApp messages."""
    if doc.type != "Incoming":
        return

    mobile = doc.from_field if hasattr(doc, "from_field") else doc.get("from")
    msg_text = (doc.message or "").strip().upper()

    if msg_text in ("OK", "ACCEPT"):
        order_name = frappe.db.get_value(
            "POS Order",
            {"whatsapp_status": "Sent", "docstatus": 0},
            order_by="creation desc",
        )
        if order_name:
            frappe.db.set_value("POS Order", order_name, "whatsapp_status", "Confirmed")
            frappe.db.set_value("POS Order", order_name, "kitchen_status", "Processing")
            _send_wa_reply(mobile, "✅ Order accepted! We are preparing your food.")
            frappe.log_error(f"Order {order_name} accepted via WhatsApp", "WhatsApp")
            return

    auto_reply = (
        "Thank you for messaging Luuv Fryxo! 🎉\n\n"
        "View your orders: https://luuvgrand.com/myorders\n"
        "To confirm a pending order, reply OK\n\n"
        "We will get back to you shortly!"
    )
    _send_wa_reply(mobile, auto_reply)
    frappe.log_error(f"Auto-reply sent to {mobile}: {msg_text}", "WhatsApp")


def _send_wa_reply(mobile, message):
    """Send a text reply via WhatsApp."""
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return
    try:
        import requests
        acc = frappe.get_doc("WhatsApp Account", wa_account)
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "text",
            "text": {"body": message},
        }
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        frappe.log_error(f"WA reply error: {str(e)}", "WhatsApp")


# ─── POS Cash Movements (drawer pay-in / pay-out) ────────
# ERPNext has no native mid-shift cash in/out; the Frappe-default way to persist a new
# record type is a DocType. POS Cash Movement is a per-shift drawer log (not GL-integrated).

@frappe.whitelist()
def setup_cash_movement():
    """Create the `POS Cash Movement` Custom DocType (idempotent; custom:1 => no bench migrate).
    A small drawer pay-in / pay-out log linked to the shift's POS Opening Entry."""
    if frappe.db.exists("DocType", "POS Cash Movement"):
        return {"created": False, "exists": True}

    # Cashier role is used by the POS; ensure it exists (mirrors setup_pos_shift_perms).
    if not frappe.db.exists("Role", "Cashier"):
        frappe.get_doc({"doctype": "Role", "role_name": "Cashier", "desk_access": 1}).insert(
            ignore_permissions=True
        )

    dt = frappe.get_doc({
        "doctype": "DocType",
        "name": "POS Cash Movement",
        "module": "Zeloura",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "track_changes": 1,
        "fields": [
            {"fieldname": "pos_opening_entry", "label": "POS Opening Entry", "fieldtype": "Link",
             "options": "POS Opening Entry", "in_list_view": 1},
            {"fieldname": "direction", "label": "Direction", "fieldtype": "Select",
             "options": "in\nout", "reqd": 1, "in_list_view": 1},
            {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1,
             "in_list_view": 1},
            {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
            {"fieldname": "cashier", "label": "Cashier", "fieldtype": "Link", "options": "User",
             "in_list_view": 1},
            {"fieldname": "movement_time", "label": "Time", "fieldtype": "Datetime",
             "in_list_view": 1},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Cashier", "read": 1, "write": 1, "create": 1},
        ],
    })
    dt.flags.ignore_permissions = True
    dt.insert()
    frappe.clear_cache()
    return {"created": True}


@frappe.whitelist(methods=["POST"])
def record_cash_movement():
    """Record a drawer pay-in or pay-out against the current open shift."""
    _require_pos_role()
    data = frappe.local.form_dict
    direction = (data.get("direction") or "").strip().lower()
    if direction not in ("in", "out"):
        frappe.throw(_("Direction must be 'in' or 'out'"))
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero"))
    reason = (data.get("reason") or "").strip()

    # The user's own open shift, else the single open shift (so a manager on the app can record).
    opening = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name") \
        or frappe.db.get_value("POS Opening Entry", {"docstatus": 1, "status": "Open"}, "name")
    if not opening:
        frappe.throw(_("No open shift — open a shift first"))

    if not frappe.db.exists("DocType", "POS Cash Movement"):
        setup_cash_movement()

    doc = frappe.get_doc({
        "doctype": "POS Cash Movement",
        "pos_opening_entry": opening,
        "direction": direction,
        "amount": amount,
        "reason": reason,
        "cashier": frappe.session.user,
        "movement_time": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    return {"ok": True, "name": doc.name, "direction": direction, "amount": amount}


@frappe.whitelist()
def get_cash_movements():
    """Cash-drawer movements for the current open shift: totals + recent list (newest first)."""
    _require_pos_role()
    opening = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name") \
        or frappe.db.get_value("POS Opening Entry", {"docstatus": 1, "status": "Open"}, "name")
    if not opening or not frappe.db.exists("DocType", "POS Cash Movement"):
        return {"in_total": 0, "out_total": 0, "moves": []}

    rows = frappe.get_all("POS Cash Movement",
        filters={"pos_opening_entry": opening},
        fields=["name", "direction", "amount", "reason", "cashier", "movement_time"],
        order_by="movement_time desc")
    in_total = sum(float(r.amount or 0) for r in rows if r.direction == "in")
    out_total = sum(float(r.amount or 0) for r in rows if r.direction == "out")
    moves = [{
        "direction": r.direction,
        "amount": float(r.amount or 0),
        "reason": r.reason or "",
        "cashier": frappe.get_value("User", r.cashier, "full_name") or r.cashier,
        "time": frappe.utils.pretty_date(r.movement_time),
    } for r in rows]
    return {"in_total": in_total, "out_total": out_total, "moves": moves}


# ─── Move an order to another table ──────────────────────

@frappe.whitelist(methods=["POST"])
def move_order_table():
    """Move an open (unpaid) POS Order to a different restaurant table."""
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    table = (data.get("table") or "").strip()
    if not order_name:
        frappe.throw(_("Order name is required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only open orders can be moved"))
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot move a paid order"))
    if table and not frappe.db.exists("Restaurant Table", table):
        frappe.throw(_("Table {0} not found").format(table))

    old_table = pos_order.restaurant_table
    pos_order.db_set("restaurant_table", table)
    return {"ok": True, "name": order_name, "old_table": old_table or "", "table": table}


# ─── Purchases & Expenses (buying bills → manager approval → price book) ──
# A purchaser logs a supplier bill (items + purchase prices + receipt); the manager approves,
# which updates each item's BUYING Item Price (the "price book"). Frappe-default = a DocType.

def _buying_price_list():
    pl = frappe.db.get_value("Price List", {"buying": 1, "enabled": 1}, "name")
    return pl or "Standard Buying"


@frappe.whitelist()
def setup_purchase_bill():
    """Create `Purchase Bill` + `Purchase Bill Item` Custom DocTypes (idempotent; no migrate)."""
    if frappe.db.exists("DocType", "Purchase Bill"):
        return {"created": False, "exists": True}
    if not frappe.db.exists("Price List", "Standard Buying"):
        frappe.get_doc({"doctype": "Price List", "price_list_name": "Standard Buying",
                        "buying": 1, "enabled": 1, "currency": "LKR"}).insert(ignore_permissions=True)
    # child first
    frappe.get_doc({
        "doctype": "DocType", "name": "Purchase Bill Item", "module": "Zeloura",
        "custom": 1, "istable": 1, "editable_grid": 1,
        "fields": [
            {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
            {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data", "in_list_view": 1},
            {"fieldname": "uom", "label": "UOM", "fieldtype": "Data"},
            {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "in_list_view": 1},
            {"fieldname": "price", "label": "Price", "fieldtype": "Currency", "in_list_view": 1},
            {"fieldname": "old_price", "label": "Old Price", "fieldtype": "Currency"},
        ],
        "permissions": [],
    }).insert(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "DocType", "name": "Purchase Bill", "module": "Zeloura",
        "custom": 1, "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
        "fields": [
            {"fieldname": "vendor", "label": "Vendor", "fieldtype": "Data", "in_list_view": 1, "reqd": 1},
            {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
            {"fieldname": "receipt_image", "label": "Receipt", "fieldtype": "Attach Image"},
            {"fieldname": "total", "label": "Total", "fieldtype": "Currency", "in_list_view": 1},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select",
             "options": "pending\napproved\nrejected", "default": "pending", "in_list_view": 1},
            {"fieldname": "items", "label": "Items", "fieldtype": "Table", "options": "Purchase Bill Item"},
            {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
            {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Cashier", "read": 1, "write": 1, "create": 1},
        ],
    }).insert(ignore_permissions=True)
    frappe.clear_cache()
    return {"created": True}


RAW_MATERIALS = [
    ("RM-TOMATO", "Tomatoes", "Kg", 320), ("RM-LETTUCE", "Lettuce", "Kg", 280),
    ("RM-ONION", "Onions", "Kg", 240), ("RM-POTATO", "Potatoes", "Kg", 220),
    ("RM-CARROT", "Carrots", "Kg", 260), ("RM-CHICKEN", "Chicken", "Kg", 1150),
    ("RM-BEEF", "Beef mince", "Kg", 1800), ("RM-FISH", "Fish", "Kg", 1400),
    ("RM-EGGS", "Eggs", "Dozen", 600), ("RM-CHEESE", "Cheddar cheese", "Kg", 2400),
    ("RM-MILK", "Milk", "Litre", 380), ("RM-BUTTER", "Butter", "Kg", 2200),
    ("RM-BUNS", "Burger buns", "Dozen", 420), ("RM-RICE", "Rice", "Kg", 280),
    ("RM-FLOUR", "Flour", "Kg", 220), ("RM-OIL", "Cooking oil", "Litre", 650),
    ("RM-SUGAR", "Sugar", "Kg", 260), ("RM-SALT", "Salt", "Kg", 120),
    ("RM-GAS", "LP gas", "Cylinder", 4900), ("RM-BOXES", "Takeaway boxes", "Pack", 1800),
    ("RM-CUPS", "Paper cups", "Pack", 900), ("RM-NAPKINS", "Napkins", "Pack", 450),
]


@frappe.whitelist()
def setup_raw_materials():
    """Create a `Raw Materials` Item Group + restaurant ingredients (is_purchase_item) with
    their buying Item Prices. Idempotent."""
    if not frappe.db.exists("Item Group", "Raw Materials"):
        parent = frappe.db.get_value("Item Group", {"name": "All Item Groups"}, "name") \
            or frappe.db.get_value("Item Group", {"is_group": 1}, "name")
        frappe.get_doc({"doctype": "Item Group", "item_group_name": "Raw Materials",
                        "parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)
    for u in ["Kg", "Litre", "Dozen", "Cylinder", "Pack"]:
        if not frappe.db.exists("UOM", u):
            frappe.get_doc({"doctype": "UOM", "uom_name": u}).insert(ignore_permissions=True)
    if not frappe.db.exists("Price List", "Standard Buying"):
        frappe.get_doc({"doctype": "Price List", "price_list_name": "Standard Buying",
                        "buying": 1, "enabled": 1, "currency": "LKR"}).insert(ignore_permissions=True)
    pl = _buying_price_list()
    created = 0
    for code, name, uom, price in RAW_MATERIALS:
        if not frappe.db.exists("Item", code):
            frappe.get_doc({
                "doctype": "Item", "item_code": code, "item_name": name,
                "item_group": "Raw Materials", "stock_uom": uom,
                "is_purchase_item": 1, "is_sales_item": 0, "is_stock_item": 0,
            }).insert(ignore_permissions=True)
            created += 1
        if not frappe.db.exists("Item Price", {"item_code": code, "price_list": pl}):
            frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": pl,
                            "buying": 1, "price_list_rate": price}).insert(ignore_permissions=True)
    frappe.clear_cache()
    return {"created": created, "group": "Raw Materials", "total": len(RAW_MATERIALS)}


@frappe.whitelist()
def get_purchase_catalog():
    """Buying items + current purchase price (the price book) — raw materials first."""
    pl = _buying_price_list()
    items = frappe.get_all("Item", filters={"item_group": "Raw Materials", "disabled": 0},
                           fields=["name", "item_name", "stock_uom", "item_group"],
                           order_by="item_name", limit_page_length=300)
    if not items:
        items = frappe.get_all("Item", filters={"is_purchase_item": 1, "disabled": 0},
                               fields=["name", "item_name", "stock_uom", "item_group"],
                               order_by="item_name", limit_page_length=300)
    if not items:
        items = frappe.get_all("Item", filters={"disabled": 0},
                               fields=["name", "item_name", "stock_uom", "item_group"],
                               order_by="item_name", limit_page_length=120)
    out = []
    for it in items:
        price = frappe.db.get_value("Item Price", {"item_code": it.name, "price_list": pl},
                                    "price_list_rate") or 0
        out.append({"item": it.name, "name": it.item_name or it.name,
                    "unit": it.stock_uom or "Nos", "group": it.item_group or "",
                    "price": float(price)})
    return {"items": out, "price_list": pl}


@frappe.whitelist(methods=["POST"])
def create_purchase_item():
    """Create a new buying Item from the purchases app (name + unit).
    Reuses an existing Item if one with the same name is already there."""
    _require_pos_role()
    data = frappe.local.form_dict
    item_name = (data.get("item_name") or "").strip()
    if not item_name:
        frappe.throw(_("Item name is required"))
    unit = (data.get("unit") or "Nos").strip() or "Nos"

    existing = frappe.db.get_value("Item", {"item_name": item_name},
                                   ["name", "stock_uom"], as_dict=True)
    if existing:
        return {"ok": True, "item": existing.name, "name": item_name,
                "unit": existing.stock_uom or unit, "price": 0, "existed": True}

    if not frappe.db.exists("UOM", unit):
        frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert(ignore_permissions=True)
    group = "Raw Materials" if frappe.db.exists("Item Group", "Raw Materials") \
        else (frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups")
    doc = frappe.get_doc({
        "doctype": "Item",
        "item_code": item_name,
        "item_name": item_name,
        "item_group": group,
        "stock_uom": unit,
        "is_stock_item": 0,
        "is_purchase_item": 1,
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    return {"ok": True, "item": doc.name, "name": doc.item_name,
            "unit": doc.stock_uom, "price": 0}


def _bill_dict(name):
    doc = frappe.get_doc("Purchase Bill", name)
    lines = [{"item": i.item, "name": i.item_name or i.item, "unit": i.uom or "Nos",
              "qty": float(i.qty or 0), "price": float(i.price or 0),
              "old_price": float(i.old_price or 0)} for i in (doc.get("items") or [])]
    total = sum(l["qty"] * l["price"] for l in lines)
    return {
        "name": doc.name, "vendor": doc.vendor or "", "note": doc.note or "",
        "receipt_image": doc.receipt_image or "", "status": doc.status or "pending",
        "by": frappe.get_value("User", doc.requested_by, "full_name") or doc.requested_by or "",
        "time": frappe.utils.pretty_date(doc.requested_at) if doc.requested_at else "",
        "lines": lines, "total": total,
    }


@frappe.whitelist(methods=["POST"])
def create_purchase_bill():
    _require_pos_role()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        setup_purchase_bill()
    data = frappe.local.form_dict
    vendor = (data.get("vendor") or "").strip()
    if not vendor:
        frappe.throw(_("Vendor is required"))
    lines = frappe.parse_json(data.get("lines") or "[]")
    if not lines:
        frappe.throw(_("Add at least one item"))
    doc = frappe.get_doc({
        "doctype": "Purchase Bill", "vendor": vendor, "note": (data.get("note") or "").strip(),
        "receipt_image": data.get("receipt_image") or "", "status": "pending",
        "requested_by": frappe.session.user, "requested_at": now_datetime(), "items": [],
    })
    total = 0
    appr_lines = []
    for ln in lines:
        item = ln.get("item")
        qty = float(ln.get("qty") or 0)
        price = float(ln.get("price") or 0)
        old_price = frappe.db.get_value("Item Price",
            {"item_code": item, "price_list": _buying_price_list()}, "price_list_rate") or 0
        name = frappe.db.get_value("Item", item, "item_name") or item
        uom = ln.get("unit") or frappe.db.get_value("Item", item, "stock_uom") or "Nos"
        doc.append("items", {"item": item, "item_name": name, "uom": uom, "qty": qty,
                             "price": price, "old_price": float(old_price)})
        total += qty * price
        appr_lines.append({"qtyStr": f"{qty:g} {uom}", "name": name,
                           "totalStr": f"Rs {int(round(qty * price)):,}"})
    doc.total = total
    doc.flags.ignore_permissions = True
    doc.insert()

    # Route the bill to the manager app for approval (same POS Approval pipeline).
    if not frappe.db.exists("DocType", "POS Approval"):
        setup_pos_approval()
    n = len(appr_lines)
    frappe.get_doc({
        "doctype": "POS Approval", "approval_type": "bill", "title": vendor,
        "subhead": f"{n} item{'' if n == 1 else 's'} · purchase bill",
        "details_json": json.dumps({"lines": appr_lines, "footer_label": "Bill total",
                                    "footer_value": f"Rs {int(round(total)):,}"}),
        "flag_text": ((data.get("note") or "").strip()
                      or "Purchase bill — approving updates the price book"),
        "action": "purchase_bill", "action_payload": json.dumps({"bill": doc.name}),
        "status": "pending", "requested_by": frappe.session.user, "requested_at": now_datetime(),
    }).insert(ignore_permissions=True)
    return {"ok": True, "name": doc.name, "total": total}


@frappe.whitelist()
def get_purchase_bills():
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"bills": [], "approved_today": 0, "pending_amt": 0, "week": 0}
    rows = frappe.get_all("Purchase Bill", filters={}, fields=["name", "status", "requested_at"],
                          order_by="requested_at desc", limit_page_length=60)
    bills = [_bill_dict(r.name) for r in rows]
    today = frappe.utils.today()
    wk = frappe.utils.add_days(today, -7)
    approved_today = sum(b["total"] for b, r in zip(bills, rows)
                         if b["status"] == "approved" and str(r.requested_at) >= today)
    pending_amt = sum(b["total"] for b in bills if b["status"] == "pending")
    week = sum(b["total"] for b, r in zip(bills, rows)
               if b["status"] != "rejected" and str(r.requested_at) >= wk)
    return {"bills": bills, "approved_today": approved_today, "pending_amt": pending_amt, "week": week}


@frappe.whitelist()
def get_price_book():
    cat = get_purchase_catalog()
    return cat


def _decide_purchase_bill(name, decision):
    """Apply a manager decision to a Purchase Bill (idempotent); approving updates
    each item's buying Item Price. Returns the number of prices changed."""
    doc = frappe.get_doc("Purchase Bill", name)
    if doc.status != "pending":
        return 0
    updated = 0
    if decision == "approved":
        pl = _buying_price_list()
        for line in (doc.get("items") or []):
            if not line.item or not line.price:
                continue
            existing = frappe.db.get_value("Item Price",
                {"item_code": line.item, "price_list": pl}, "name")
            if existing:
                if float(frappe.db.get_value("Item Price", existing, "price_list_rate") or 0) != float(line.price):
                    frappe.db.set_value("Item Price", existing, "price_list_rate", float(line.price))
                    updated += 1
            else:
                frappe.get_doc({"doctype": "Item Price", "item_code": line.item,
                                "price_list": pl, "buying": 1,
                                "price_list_rate": float(line.price)}).insert(ignore_permissions=True)
                updated += 1
    doc.db_set("status", decision)
    doc.db_set("resolved_by", frappe.session.user)
    doc.db_set("resolved_at", now_datetime())
    return updated


@frappe.whitelist(methods=["POST"])
def resolve_purchase_bill():
    """Manager approves/rejects a bill; approving updates each item's buying Item Price."""
    _require_pos_role()
    data = frappe.local.form_dict
    name = data.get("name")
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        frappe.throw(_("Decision must be 'approved' or 'rejected'"))
    if frappe.db.get_value("Purchase Bill", name, "status") != "pending":
        frappe.throw(_("Already resolved"))
    updated = _decide_purchase_bill(name, decision)
    return {"ok": True, "status": decision, "prices_updated": updated}


# ─── Manager dashboard (today's sales, cash drawer, approvals) ─────

@frappe.whitelist()
def get_manager_dashboard():
    """Aggregate today's POS activity for the Manager phone dashboard (all cashiers)."""
    from frappe.utils import get_datetime, today as _today
    today = _today()

    invoices = frappe.get_all("POS Invoice",
        filters={"docstatus": 1, "posting_date": today},
        fields=["name", "grand_total", "creation"])
    revenue = sum(float(i.grand_total or 0) for i in invoices)
    sales_count = len(invoices)
    avg = revenue / sales_count if sales_count else 0

    # payment mix + cash sales
    pay = {}
    cash_sales = 0
    for inv in invoices:
        doc = frappe.get_doc("POS Invoice", inv.name)
        for p in (doc.payments or []):
            mode = p.mode_of_payment or "Other"
            amt = float(p.amount or 0)
            pay[mode] = pay.get(mode, 0) + amt
            if mode == "Cash":
                cash_sales += amt
    payments = [{"mode": m, "amount": a} for m, a in sorted(pay.items(), key=lambda x: -x[1])]

    # hourly revenue
    hourly = {}
    for inv in invoices:
        h = get_datetime(inv.creation).hour
        hourly[h] = hourly.get(h, 0) + float(inv.grand_total or 0)
    hourly_list = [{"hour": h, "amount": hourly[h]} for h in sorted(hourly.keys())]

    # top items today (from paid POS Orders)
    paid_orders = frappe.get_all("POS Order",
        filters={"pos_invoice": ["!=", ""], "creation": [">=", today]}, pluck="name")
    tally = {}
    for name in paid_orders:
        od = frappe.get_doc("POS Order", name)
        for it in (od.get("items") or []):
            key = it.item_name or it.item
            t = tally.setdefault(key, {"qty": 0, "rev": 0})
            t["qty"] += int(it.qty or 0)
            t["rev"] += float(it.rate or 0) * int(it.qty or 0)
    top_items = sorted(
        [{"name": k, "qty": v["qty"], "revenue": v["rev"]} for k, v in tally.items()],
        key=lambda x: -x["revenue"])[:5]

    # voids today
    voids = frappe.get_all("POS Order",
        filters={"docstatus": 2, "kitchen_status": "Cancelled", "modified": [">=", today]},
        fields=["grand_total"])
    void_count = len(voids)
    void_amt = sum(float(v.grand_total or 0) for v in voids)

    # cash drawer — the open shift (any cashier)
    opening = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open"}, ["name", "period_start_date", "user"], as_dict=True)
    float_amt = 0
    cash_in = cash_out = 0
    cash_moves = []
    opened_at = ""
    cashier = ""
    if opening:
        od = frappe.get_doc("POS Opening Entry", opening.name)
        float_amt = sum(float(d.opening_amount or 0) for d in (od.balance_details or []))
        opened_at = str(opening.period_start_date)
        cashier = frappe.get_value("User", opening.user, "full_name") or opening.user
        if frappe.db.exists("DocType", "POS Cash Movement"):
            rows = frappe.get_all("POS Cash Movement",
                filters={"pos_opening_entry": opening.name},
                fields=["direction", "amount", "reason", "cashier", "movement_time"],
                order_by="movement_time desc")
            for r in rows:
                amt = float(r.amount or 0)
                if r.direction == "in":
                    cash_in += amt
                else:
                    cash_out += amt
                cash_moves.append({
                    "direction": r.direction, "amount": amt, "reason": r.reason or "",
                    "cashier": frappe.get_value("User", r.cashier, "full_name") or r.cashier,
                    "time": frappe.utils.pretty_date(r.movement_time),
                })
    expected_drawer = float_amt + cash_sales + cash_in - cash_out

    return {
        "date": today,
        "revenue": revenue,
        "sales_count": sales_count,
        "avg_ticket": avg,
        "void_count": void_count,
        "void_amount": void_amt,
        "payments": payments,
        "hourly": hourly_list,
        "top_items": top_items,
        "cash": {
            "opened_at": opened_at,
            "cashier": cashier,
            "float": float_amt,
            "cash_sales": cash_sales,
            "cash_in": cash_in,
            "cash_out": cash_out,
            "expected": expected_drawer,
            "moves": cash_moves,
        },
        "approvals": _format_approvals(_pending_and_recent_approvals()),
    }


@frappe.whitelist()
def get_manager_sales_report(days=7):
    """Daily sales trend + item-wise sales over the last N days (all cashiers).

    Powers the Manager app 'Reports' tab: a day-by-day revenue chart plus a
    full item-wise breakdown for the same window.
    """
    from frappe.utils import getdate, add_days, today as _today, formatdate

    try:
        days = int(days)
    except Exception:
        days = 7
    days = max(1, min(days, 92))  # cap the window

    end = getdate(_today())
    start = add_days(end, -(days - 1))

    # ── daily revenue + sale count (one grouped query) ──
    rows = frappe.db.sql(
        """
        SELECT posting_date AS d,
               COALESCE(SUM(grand_total), 0) AS revenue,
               COUNT(*) AS cnt
        FROM `tabPOS Invoice`
        WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s
        GROUP BY posting_date
        """,
        (start, end), as_dict=True)
    by_date = {str(r.d): {"revenue": float(r.revenue or 0), "count": int(r.cnt or 0)} for r in rows}

    daily = []
    cur = start
    while cur <= end:
        key = str(cur)
        rec = by_date.get(key, {"revenue": 0, "count": 0})
        daily.append({
            "date": key,
            "label": formatdate(cur, "d MMM"),
            "weekday": formatdate(cur, "EEE"),
            "revenue": rec["revenue"],
            "count": rec["count"],
        })
        cur = add_days(cur, 1)

    total_revenue = sum(d["revenue"] for d in daily)
    total_sales = sum(d["count"] for d in daily)
    avg_ticket = total_revenue / total_sales if total_sales else 0
    best_day = max(daily, key=lambda d: d["revenue"]) if daily else None

    # ── item-wise sales over the window (from paid POS Orders) ──
    item_rows = frappe.db.sql(
        """
        SELECT COALESCE(soi.item_name, soi.item) AS name,
               SUM(soi.qty) AS qty,
               SUM(soi.rate * soi.qty) AS revenue
        FROM `tabPOS Order Item` soi
        INNER JOIN `tabPOS Order` so ON so.name = soi.parent
        WHERE so.pos_invoice IS NOT NULL AND so.pos_invoice != ''
          AND so.creation >= %s AND so.creation < %s
        GROUP BY name
        ORDER BY revenue DESC
        """,
        (start, add_days(end, 1)), as_dict=True)
    items = [{"name": r.name, "qty": int(r.qty or 0), "revenue": float(r.revenue or 0)}
             for r in item_rows if r.name]
    items_revenue = sum(i["revenue"] for i in items) or 1

    return {
        "days": days,
        "start": str(start),
        "end": str(end),
        "daily": daily,
        "items": items,
        "items_revenue": items_revenue,
        "total_revenue": total_revenue,
        "total_sales": total_sales,
        "avg_ticket": avg_ticket,
        "best_day": best_day,
    }


# ─── POS Approvals (manager sign-off for bill changes: voids, price overrides) ─
# When the POS makes a change that needs a manager (void after guest check, price
# override, etc.) it raises a POS Approval; the Manager app approves/rejects and the
# action is executed server-side. The Frappe-default way: a dedicated DocType.

@frappe.whitelist()
def setup_pos_approval():
    """Create the `POS Approval` Custom DocType (idempotent; custom:1, no migrate)."""
    if frappe.db.exists("DocType", "POS Approval"):
        return {"created": False, "exists": True}
    if not frappe.db.exists("Role", "Cashier"):
        frappe.get_doc({"doctype": "Role", "role_name": "Cashier", "desk_access": 1}).insert(
            ignore_permissions=True)
    dt = frappe.get_doc({
        "doctype": "DocType", "name": "POS Approval", "module": "Zeloura", "custom": 1,
        "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
        "fields": [
            {"fieldname": "approval_type", "label": "Type", "fieldtype": "Select",
             "options": "void\nprice\nitem\nbill\ndiscount\nedit", "in_list_view": 1},
            {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link",
             "options": "POS Order", "in_list_view": 1},
            {"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
            {"fieldname": "subhead", "label": "Subhead", "fieldtype": "Small Text"},
            {"fieldname": "details_json", "label": "Details JSON", "fieldtype": "Long Text"},
            {"fieldname": "flag_text", "label": "Flag", "fieldtype": "Data"},
            {"fieldname": "action", "label": "Action", "fieldtype": "Data"},
            {"fieldname": "action_payload", "label": "Action Payload", "fieldtype": "Long Text"},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select",
             "options": "pending\napproved\nrejected", "default": "pending", "in_list_view": 1},
            {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
            {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Cashier", "read": 1, "write": 1, "create": 1},
        ],
    })
    dt.flags.ignore_permissions = True
    dt.insert()
    frappe.clear_cache()
    return {"created": True}


@frappe.whitelist(methods=["POST"])
def create_approval():
    """POS raises an approval request for a bill change that needs a manager."""
    _require_pos_role()
    if not frappe.db.exists("DocType", "POS Approval"):
        setup_pos_approval()
    data = frappe.local.form_dict
    doc = frappe.get_doc({
        "doctype": "POS Approval",
        "approval_type": data.get("approval_type", "void"),
        "pos_order": data.get("pos_order", ""),
        "title": data.get("title", "") or "Approval request",
        "subhead": data.get("subhead", ""),
        "details_json": data.get("details_json", "") or "",
        "flag_text": data.get("flag_text", ""),
        "action": data.get("action", ""),
        "action_payload": data.get("action_payload", "") or "",
        "status": "pending",
        "requested_by": frappe.session.user,
        "requested_at": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    return {"ok": True, "name": doc.name}


def _pending_and_recent_approvals():
    if not frappe.db.exists("DocType", "POS Approval"):
        return []
    fields = ["name", "approval_type", "pos_order", "title", "subhead", "details_json",
              "flag_text", "status", "requested_by", "requested_at", "resolved_at"]
    pending = frappe.get_all("POS Approval", filters={"status": "pending"},
                             fields=fields, order_by="requested_at desc")
    recent = frappe.get_all("POS Approval",
                            filters=[["status", "in", ["approved", "rejected"]],
                                     ["modified", ">=", frappe.utils.today()]],
                            fields=fields, order_by="modified desc", limit_page_length=15)
    return pending + recent


def _format_approvals(rows):
    out = []
    for r in rows:
        try:
            details = json.loads(r.get("details_json") or "{}")
        except Exception:
            details = {}
        out.append({
            "name": r.get("name"),
            "type": r.get("approval_type") or "item",
            "pos_order": r.get("pos_order") or "",
            "title": r.get("title") or "",
            "subhead": r.get("subhead") or "",
            "lines": details.get("lines") or [],
            "footer_label": details.get("footer_label") or "",
            "footer_value": details.get("footer_value") or "",
            "flag_text": r.get("flag_text") or "",
            "status": r.get("status") or "pending",
            "time": frappe.utils.pretty_date(r.get("requested_at")),
        })
    return out


@frappe.whitelist()
def get_pending_approvals():
    rows = _pending_and_recent_approvals()
    pending = sum(1 for r in rows if r.get("status") == "pending")
    return {"approvals": _format_approvals(rows), "pending": pending}


@frappe.whitelist()
def get_approval_status(name):
    if not name or not frappe.db.exists("DocType", "POS Approval"):
        return {"status": "unknown"}
    return {"status": frappe.db.get_value("POS Approval", name, "status") or "unknown"}


@frappe.whitelist(methods=["POST"])
def resolve_approval():
    """Manager approves/rejects a POS Approval; on approve the action is executed."""
    _require_pos_role()
    data = frappe.local.form_dict
    name = data.get("name")
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        frappe.throw(_("Decision must be 'approved' or 'rejected'"))
    doc = frappe.get_doc("POS Approval", name)
    if doc.status != "pending":
        frappe.throw(_("Already resolved"))
    doc.db_set("status", decision)
    doc.db_set("resolved_by", frappe.session.user)
    doc.db_set("resolved_at", now_datetime())

    executed = None
    action = (doc.action or "")
    try:
        if action == "purchase_bill":
            payload = json.loads(doc.action_payload or "{}")
            bill = payload.get("bill")
            if bill and frappe.db.exists("Purchase Bill", bill):
                _decide_purchase_bill(bill, decision)
                executed = "purchase_" + decision
        elif decision == "approved" and doc.pos_order:
            if action == "void":
                po = frappe.get_doc("POS Order", doc.pos_order)
                if po.docstatus == 0 and not po.pos_invoice:
                    old_status = po.kitchen_status
                    po.db_set("docstatus", 2)
                    po.db_set("kitchen_status", "Cancelled")
                    frappe.get_doc({
                        "doctype": "Version", "ref_doctype": "POS Order", "docname": doc.pos_order,
                        "data": frappe.as_json({"changed": [
                            ["docstatus", "0", "2"],
                            ["kitchen_status", old_status or "Pending", "Cancelled"]]}),
                        "owner": frappe.session.user, "modified_by": frappe.session.user,
                        "creation": now_datetime(),
                    }).insert(ignore_permissions=True)
                    executed = "voided"
            elif action == "update_items":
                payload = json.loads(doc.action_payload or "{}")
                items = payload.get("items") or []
                _apply_order_items(doc.pos_order, items)
                executed = "items_updated"
            elif action == "discount":
                payload = json.loads(doc.action_payload or "{}")
                _apply_order_discount(doc.pos_order, payload)
                executed = "discounted"
    except Exception:
        frappe.log_error(frappe.get_traceback(), "resolve_approval action failed")
    return {"ok": True, "status": decision, "executed": executed}


def _apply_order_discount(order_name, payload):
    """Apply a manager-approved discount to a draft POS Order. The discount comes off the
    items subtotal (before service charge); a percentage is converted to an amount here."""
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0 or pos_order.pos_invoice:
        return
    subtotal = sum((i.qty or 1) * (i.rate or 0) for i in pos_order.items)
    kind = payload.get("kind") or "amt"
    try:
        value = float(payload.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    if kind == "pct":
        amount = round(subtotal * value / 100, 2)
        note = ("%g%%" % value)
    else:
        amount = round(value, 2)
        note = "Rs." + frappe.utils.fmt_money(amount, 0)
    if amount > subtotal:
        amount = subtotal
    pos_order.db_set("discount_amount", amount)
    pos_order.db_set("discount_note", note)
    _apply_takeaway_service_charge(pos_order)


def _apply_order_items(order_name, items):
    """Replace a draft POS Order's items + recompute service charge (shared by update_order
    and approved bill-edits)."""
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only draft orders can be edited"))
    order_type = pos_order.order_type or ""
    pos_order.items = []
    subtotal = 0
    sc_base = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty,
                                   "rate": rate, "takeaway": 1 if item_ta else 0})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty
    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.save()
    _apply_takeaway_service_charge(pos_order)
    return pos_order


@frappe.whitelist()
def setup_check_printed():
    """Add a `check_printed` Check to POS Order (set when a guest check is printed; after that,
    deleting items needs a manager approval). Idempotent; no migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field
    created = False
    if not frappe.db.has_column("POS Order", "check_printed"):
        create_custom_field("POS Order", {
            "fieldname": "check_printed", "label": "Guest Check Printed",
            "fieldtype": "Check", "default": "0", "insert_after": "kitchen_status"})
        created = True
    frappe.clear_cache()
    return {"check_printed_created": created}


@frappe.whitelist(methods=["POST"])
def mark_check_printed():
    """Flag that a guest check was printed for an order."""
    _require_pos_role()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not frappe.db.has_column("POS Order", "check_printed"):
        setup_check_printed()
    frappe.db.set_value("POS Order", order_name, "check_printed", 1)
    return {"ok": True}


@frappe.whitelist()
def get_order_flags(order_name):
    """Lightweight flags the POS needs to decide if an edit requires approval."""
    if not order_name:
        return {}
    cp = 0
    if frappe.db.has_column("POS Order", "check_printed"):
        cp = int(frappe.db.get_value("POS Order", order_name, "check_printed") or 0)
    return {"check_printed": cp}