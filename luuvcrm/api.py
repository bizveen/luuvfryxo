import frappe
from frappe import _
import json
import secrets
import hashlib
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

def _shift_unconsolidated_invoices(user, pos_profile):
    """Every submitted, UNCONSOLIDATED POS Invoice for this cashier+profile — i.e. the open
    shift's sales. We attribute by "unconsolidated" rather than ERPNext's [period_start, now]
    time window: a window silently drops any bill whose posting timestamp falls outside it (a
    sale paid right at/just before a previous close, clock skew, offline replay) which makes the
    close show Rs 0 / undercount. Closed shifts already consolidated theirs, so this is exactly
    the current shift's set."""
    return frappe.db.sql("""
        select name, grand_total
        from `tabPOS Invoice`
        where owner = %s and docstatus = 1 and pos_profile = %s
          and ifnull(consolidated_invoice, '') = ''
        order by timestamp(posting_date, posting_time)
    """, (user, pos_profile), as_dict=True)


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
        # Deterministic: a table with several open orders binds its tile to the
        # OLDEST one (matches get_table_orders); without this the pick is arbitrary.
        order_by="creation asc",
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
        pos_items.append({"item": item_code, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0,
                          "notes": _clean_item_note(item_data.get("notes"))})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    grand_total = subtotal + sc_amount

    # Guide balance redemption (guide eats, pays from cashback): validated
    # up-front so an invalid redemption fails the payment loudly.
    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), grand_total)
    net_due = round(grand_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

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
    if redeem_amt > 0:
        # the redeemed part rides as a Net Total discount so the invoice's grand
        # total equals what the customer actually pays (POS Invoice requires
        # payments to cover the full grand total on submit)
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = redeem_amt
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), net_due)

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

    _ensure_item_notes_field()
    for item in pos_items:
        item_name = frappe.db.get_value("Item", item["item"], "item_name") or item["item"]
        pos_order.append("items", {
            "item": item["item"],
            "item_name": item_name,
            "qty": item["qty"],
            "rate": item["rate"],
            "takeaway": item.get("takeaway", 0),
            "notes": _clean_item_note(item.get("notes")),
        })

    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)
    _apply_takeaway_service_charge(pos_order)

    _link_invoice_to_order(invoice.name, pos_order.name)

    _finalize_guide_redeem(redeem_guide, redeem_amt, pos_order.name, invoice.name)
    _credit_guide_cashback(data.get("guides"),
        grand_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE else subtotal,
        pos_order.name, invoice.name, items=pos_items)

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
    country_code = (data.get("country_code") or "94").strip() or "94"   # dial code for the WhatsApp number
    table = data.get("table", "")
    notes = data.get("notes", "").strip()
    order_source = data.get("order_source", "Walk-in")
    order_type = data.get("order_type", "")
    amended_from = data.get("amended_from", "").strip()
    waiter_name = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    # Draft = auto-created as the cashier builds the ticket; not yet fired to the kitchen.
    is_draft = int(data.get("draft") or 0)

    # Dedupe guard: a lost response / retry / second tab can re-send the same autosave
    # create and orphan a duplicate draft on the table. Only an IDENTICAL payload
    # (same item/takeaway/qty lines) within 20s is treated as the same create — a
    # deliberate second ticket on the table (different items) is never swallowed.
    if is_draft and table:
        from frappe.utils import add_to_date, now_datetime
        incoming = sorted((it.get("item"), 1 if _item_is_takeaway(it, order_type) else 0,
                           max(int(it.get("qty", 1)), 1)) for it in items)
        for dup_name in frappe.get_all("POS Order", filters={
                "docstatus": 0, "kitchen_status": "Draft", "restaurant_table": table,
                "owner": frappe.session.user, "pos_invoice": ["in", ["", None]],
                "creation": [">", add_to_date(now_datetime(), seconds=-20)]},
                order_by="creation desc", pluck="name"):
            dup = frappe.get_doc("POS Order", dup_name)
            if any(int(i.get("sent_qty") or 0) for i in (dup.get("items") or [])):
                continue
            existing = sorted((i.item, int(i.get("takeaway") or 0), int(i.qty or 0))
                              for i in (dup.get("items") or []))
            if existing == incoming:
                return {
                    "name": dup.name,
                    "grand_total": dup.grand_total,
                    "subtotal": sum(float(i.rate or 0) * int(i.qty or 0) for i in dup.items),
                    "service_charge_rate": dup.service_charge_rate,
                    "service_charge_amount": dup.service_charge_amount,
                    "table": table,
                    "customer_name": dup.customer_name,
                    "deduped": True,
                }

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

    _ensure_item_notes_field()
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
                                   "notes": _clean_item_note(item_data.get("notes"))})
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

    # NOTE: the customer WhatsApp confirmation is sent client-side via a wa.me click-to-chat link
    # (waiter's WhatsApp opens with the message pre-filled), NOT the backend Cloud API — this
    # avoids Meta's 24h-window / approved-template requirement. `country_code` is returned so the
    # client can build the international wa.me number.

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
        "country_code": country_code,
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

    order_total = float(pos_order.grand_total or 0)
    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), order_total)
    net_due = round(order_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

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
    total_disc = float(pos_order.get("discount_amount") or 0) + redeem_amt
    if total_disc > 0:
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = total_disc
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), net_due)

    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()  # finalize the sale so it consolidates at shift close

    pos_order.db_set("pos_invoice", invoice.name)
    _link_invoice_to_order(invoice.name, order_name)

    _finalize_guide_redeem(redeem_guide, redeem_amt, order_name, invoice.name)
    _credit_guide_cashback(data.get("guides"),
        order_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE
        else order_total - float(pos_order.service_charge_amount or 0),
        order_name, invoice.name,
        items=[{"item": i.item, "qty": i.qty, "rate": i.rate} for i in pos_order.items])

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

    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), grand_total)
    net_due = round(grand_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

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
    if redeem_amt > 0:
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = redeem_amt
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

    _finalize_guide_redeem(redeem_guide, redeem_amt, order_name, invoice.name)
    _credit_guide_cashback(data.get("guides"),
        grand_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE else subtotal,
        order_name, invoice.name,
        items=[{"item": ii["item_code"], "qty": ii["qty"], "rate": ii["rate"]}
               for ii in invoice_items])

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
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate, "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0), "notes": i.get("notes") or ""} for i in (doc.get("items") or [])])
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
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate, "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0), "notes": i.get("notes") or ""} for i in (doc.get("items") or [])])
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
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name",
        order_by="creation desc")
    if not opening_name:
        frappe.throw(_("No open shift found"))

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    # ERPNext gathers the closing's invoices over [period_start_date, now]. Widen the start (in
    # memory only) to the earliest unconsolidated bill for this cashier+profile so a sale paid
    # at/just before this shift opened is still captured — otherwise it's silently dropped and the
    # close shows Rs 0 / undercounts. Restore the field right after (make_closing_entry only reads
    # it) so the later opening.save() keeps the true shift start.
    _true_start = opening.period_start_date
    _earliest = frappe.db.sql("""
        select min(timestamp(posting_date, posting_time)) from `tabPOS Invoice`
        where owner = %s and docstatus = 1 and pos_profile = %s and ifnull(consolidated_invoice, '') = ''
    """, (opening.user, opening.pos_profile))[0][0]
    if _earliest and str(_earliest) < str(_true_start):
        opening.period_start_date = _earliest

    # Native: gather this shift's submitted POS Invoices into a POS Closing Entry
    # (pos_transactions + taxes + payment reconciliation built by ERPNext).
    closing = make_closing_entry_from_opening(opening)
    opening.period_start_date = _true_start  # restore so update_opening_entry doesn't persist the widened start

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
        # Sales per mode = expected - opening (closing_amount is the cashier's COUNT,
        # which drifts from sales whenever the drawer is over/short).
        payment_breakdown[d.mode_of_payment] = float(d.expected_amount) - float(d.opening_amount)
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
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name",
        order_by="creation desc")
    if not opening_name:
        return {"shift": None}

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    opening_amt = 0
    opening_details = []
    for d in opening.balance_details:
        opening_amt += float(d.opening_amount)
        opening_details.append({"mode_of_payment": d.mode_of_payment,
                                "opening_amount": float(d.opening_amount or 0)})

    # Live totals from this shift's UNCONSOLIDATED POS Invoices (attributed by user+profile, not a
    # fragile [period_start, now] window) so the close modal shows every bill — never Rs 0 while
    # sales exist. payment_breakdown sums each invoice's payment rows.
    invoices = _shift_unconsolidated_invoices(opening.user, opening.pos_profile)
    total_sales = sum(float(i.get("grand_total") or 0) for i in invoices)
    order_count = len(invoices)

    payment_breakdown = {}
    for p in frappe.db.sql("""
        select sp.mode_of_payment as mode, sum(sp.amount) as amt
        from `tabSales Invoice Payment` sp
        join `tabPOS Invoice` pi on sp.parent = pi.name
        where pi.owner = %s and pi.docstatus = 1 and pi.pos_profile = %s
          and ifnull(pi.consolidated_invoice, '') = ''
        group by sp.mode_of_payment
    """, (opening.user, opening.pos_profile), as_dict=True):
        if p.mode:
            payment_breakdown[p.mode] = float(p.amt or 0)

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
def setup_waiter_role(assign_to=None):
    """Create the 'Waiter' role and (optionally) assign it to staff users.

    The waiter app (/waiter) is gated on this role in www/waiter.py, and POS Order
    already authorizes it (pos_order.json). But no setup code ever created or assigned
    it — only 'Cashier' is auto-created — so in practice only System Manager worked.

    Idempotent. Pass `assign_to` as a list or comma-separated string of staff emails;
    ONLY explicit emails are ever granted the role (customer accounts from /cus must
    never receive it), so bulk-assign-all is deliberately not supported.
    """
    from frappe.permissions import add_permission, update_permission_property

    role = "Waiter"
    # desk_access=0: waiters use the /waiter website, not Frappe Desk. Login still works.
    if not frappe.db.exists("Role", role):
        frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0}).insert(
            ignore_permissions=True
        )

    # Make the role's POS Order perms explicit rather than relying on migrate syncing
    # pos_order.json (read/write/create — matches the doctype's Waiter perm block).
    grants = {"POS Order": ["read", "write", "create"]}
    applied = {}
    for doctype, perms in grants.items():
        add_permission(doctype, role, 0)
        for p in perms:
            update_permission_property(doctype, role, 0, p, 1)
        applied[doctype] = perms

    # Normalize assign_to to a clean list of emails.
    if isinstance(assign_to, str):
        assign_to = frappe.parse_json(assign_to) if assign_to.strip().startswith("[") \
            else [e.strip() for e in assign_to.split(",")]
    emails = [e for e in (assign_to or []) if e and str(e).strip()]

    assigned, skipped = [], []
    for email in emails:
        if not frappe.db.exists("User", email):
            skipped.append(email)
            continue
        frappe.get_doc("User", email).add_roles(role)  # idempotent
        assigned.append(email)

    frappe.clear_cache()
    return {"role": role, "granted": applied, "assigned": assigned, "skipped": skipped}


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


@frappe.whitelist()
def setup_item_notes_field():
    """Add a per-line `notes` Small Text to POS Order Item — the waiter's special instruction
    for that specific line (e.g. "no chili", "extra spicy"). Lets the same item appear as two
    lines with different prep notes. Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "notes"):
        create_custom_field("POS Order Item", {
            "fieldname": "notes",
            "label": "Notes",
            "fieldtype": "Small Text",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.notes_field_created": created}


def _ensure_item_notes_field():
    """Lazily create the per-line notes column the first time a write path needs it."""
    if not frappe.db.has_column("POS Order Item", "notes"):
        setup_item_notes_field()


def _clean_item_note(value):
    """Normalise a per-line note: trim and cap length; returns '' for empty/None."""
    return (str(value or "").strip())[:280]


@frappe.whitelist(methods=["POST"])
def mark_kot_printed():
    """Flag that the current items have been fired to the kitchen — sets each line's
    sent_qty to its qty, so the next KOT shows only items added afterwards."""
    # Any authenticated user (incl. the Waiter role, which is NOT in POS_ROLES) may fire a KOT —
    # this only advances sent_qty on an order (benign, same Draft→Pending promotion send_to_kitchen
    # already does gate-free). Whitelist alone blocks guests.
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
              "sent_qty": int(i.get("sent_qty") or 0), "notes": i.get("notes") or "",
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
                  "takeaway": int(i.get("takeaway") or 0), "sent_qty": int(i.get("sent_qty") or 0),
                  "notes": i.get("notes") or ""}
                 for i in (doc.get("items") or [])]
        o["items"] = items
    return {"orders": orders}


@frappe.whitelist()
def get_my_orders():
    """The logged-in waiter's OWN orders for today (not the whole restaurant, unlike
    get_kitchen_orders which the kitchen display needs). Powers the waiter app's 'My Orders'
    tab: shows status, lets the waiter add more items (Add items) or mark served."""
    today = frappe.utils.today()
    orders = frappe.get_all("POS Order",
        filters={"owner": frappe.session.user, "creation": [">=", today]},
        fields=["name", "restaurant_table", "kitchen_status", "grand_total", "creation",
                "order_source", "notes", "pos_invoice"],
        order_by="creation desc"
    )
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        o["paid"] = 1 if o.get("pos_invoice") else 0
        doc = frappe.get_doc("POS Order", o.name)
        o["items"] = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty,
                       "rate": i.rate, "takeaway": int(i.get("takeaway") or 0),
                       "sent_qty": int(i.get("sent_qty") or 0), "notes": i.get("notes") or ""}
                      for i in (doc.get("items") or [])]
    return {"orders": orders}


@frappe.whitelist(methods=["POST"])
def waiter_append_items():
    """Append NEW item lines to an existing unpaid draft order (waiter 'add to the bill').
    Append-only — existing/fired rows are untouched, so a KOT reprint (get_kot_print_data +
    mark_kot_printed, keyed on sent_qty) fires ONLY these new lines. This deliberately avoids
    update_order's full rebuild, which folds multiple lines of the same (item, takeaway) and
    would mis-carry sent_qty onto a freshly-added same-item line."""
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not items:
        frappe.throw(_("At least one item is required"))

    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only draft orders can be added to"))
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot add items — payment already processed"))

    _ensure_item_notes_field()
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        setup_sent_qty_field()
    # Whatever is already on the bill is considered already fired to the kitchen — so the KOT for
    # this append prints ONLY the new lines (get_kot_print_data keys on qty - sent_qty).
    for i in (pos_order.get("items") or []):
        i.sent_qty = int(i.qty or 0)

    order_type = pos_order.order_type or ""
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty,
                                   "rate": rate, "takeaway": 1 if item_ta else 0,
                                   "notes": _clean_item_note(item_data.get("notes")),
                                   "sent_qty": 0})

    # Recompute totals on the full (existing + new) line set; service charge excludes takeaway.
    subtotal = 0
    sc_base = 0
    for i in pos_order.get("items"):
        subtotal += (i.rate or 0) * (i.qty or 0)
        if not int(i.get("takeaway") or 0):
            sc_base += (i.rate or 0) * (i.qty or 0)
    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.save()
    _apply_takeaway_service_charge(pos_order)
    return {"name": pos_order.name, "grand_total": pos_order.grand_total}


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
    _fields = ["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
               "grand_total", "creation", "order_source", "notes", "kitchen_status"]
    if frappe.db.has_column("POS Order", "scheduled_time"):
        _fields.append("scheduled_time")
    orders = frappe.get_all("POS Order",
        filters={"kitchen_status": ["not in", ["Served", "Draft", "Cancelled"]]},
        fields=_fields,
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
                "group": item_group, "kitchen_group": kg_name,
                "notes": i.get("notes") or ""
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

    # Guard against the table-swap race: the client says which table it THINKS
    # this order is on; if the order actually belongs to another table, refuse
    # the full-items rebuild instead of silently mixing tickets. The marker
    # "order-table-mismatch" is matched by pos2.html to reset + refresh.
    expected_table = (data.get("expected_table") or "").strip()
    if expected_table and (pos_order.restaurant_table or "") != expected_table:
        frappe.throw(_("order-table-mismatch: Order {0} is on {1}, not {2} — refresh the table view").format(
            order_name, pos_order.restaurant_table or "no table", expected_table))

    # Track whether a receipt was already printed (invoice exists)
    was_printed = bool(pos_order.pos_invoice)

    order_type = pos_order.order_type or ""
    # Preserve per-line "already fired to kitchen" qty across the full items rebuild,
    # so a re-print of the KOT shows only the newly added items.
    # Keyed by (item, takeaway) so a same-item dine-in/takeaway split keeps each line's fired qty.
    # Also preserve each line's per-item note (pos2 doesn't send notes back, so without this a
    # cashier autosave would wipe the waiter's kitchen instructions). Distinct notes for the same
    # (item, takeaway) group are joined, since pos2 folds them into a single cart line.
    _ensure_item_notes_field()
    sent_map = {}
    notes_map = {}
    for i in (pos_order.get("items") or []):
        key = (i.item, int(i.get("takeaway") or 0))
        sent_map[key] = int(i.get("sent_qty") or 0)
        n = _clean_item_note(i.get("notes"))
        if n:
            parts = [p for p in notes_map.get(key, "").split("; ") if p]
            if n not in parts:
                parts.append(n)
            notes_map[key] = "; ".join(parts)
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
        ta_flag = 1 if item_ta else 0
        line_note = _clean_item_note(item_data.get("notes")) or notes_map.get((item_code, ta_flag), "")
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate,
                                   "takeaway": ta_flag,
                                   "sent_qty": min(sent_map.get((item_code, ta_flag), 0), qty),
                                   "notes": line_note})
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
            invoice.append("payments", {"mode_of_payment": existing_payment_mode or "Cash", "amount": pos_order.grand_total})

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
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
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
        confirm_link = ("https://wa.me/" + PROMO_WA_NUMBER + "?text="
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
        "pickup_outlet": PROMO_PICKUP_OUTLET if promo_item_name else "",
        "scheduled_time": scheduled_dt.strftime("%Y-%m-%d %H:%M:%S") if scheduled_dt else "",
        "needs_wa_confirm": wa_confirm,
        "confirm_link": confirm_link,
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

    # WhatsApp confirmation: if a code was sent for this number and not yet verified,
    # the registration can't skip it (soft gate — no OTP sent means WA is down/absent).
    cache = frappe.cache()
    if cache.get_value(f"wa_otp:{mobile}") and not cache.get_value(f"wa_verified:{mobile}"):
        return {"success": False, "error": "Confirm the WhatsApp code first"}

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
    from urllib.parse import quote
    _fields = ["name", "customer_name", "restaurant_table", "grand_total",
               "kitchen_status", "docstatus", "creation", "modified", "order_source",
               "whatsapp_status"]
    if frappe.db.has_column("POS Order", "scheduled_time"):
        _fields.append("scheduled_time")
    orders = frappe.get_all("POS Order",
        filters={"mobile": mobile},
        fields=_fields,
        order_by="creation desc"
    )

    result = []
    for o in orders:
        doc = frappe.get_doc("POS Order", o.name)
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])]
        awaiting = (o.get("whatsapp_status") or "") == "Awaiting Confirmation"
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
            "wa_status": o.get("whatsapp_status") or "",
            "scheduled_time": str(o.get("scheduled_time") or ""),
            "confirm_link": ("https://wa.me/" + PROMO_WA_NUMBER + "?text="
                             + quote(f"Confirming my order #{o.name} ✅")) if awaiting else "",
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
                    "rate": _resolve_item_rate(row.item, row.rate, _get_pos_price_list()),
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

BEVERAGE_GROUP_WORDS = ("beverage", "drink", "juice", "shake", "tea", "coffee",
                        "mojito", "smoothie", "bar")
BEVERAGE_NAME_WORDS = ("juice", "cola", "coke", "sprite", "soda", "shake", "tea",
                       "coffee", "cappuccino", "latte", "mojito", "beer", "smoothie",
                       "milo", "lassi", "faluda", "water", "nestomalt")


def _is_beverage_item(item_code):
    """Beverages print in their own section on the KOT/bill (they come from the
    bar/fridge, not the kitchen line). Item Group decides; the item name is a
    fallback for items filed under a generic group."""
    d = frappe.db.get_value("Item", item_code, ["item_group", "item_name"], as_dict=True)
    if not d:
        return False
    grp = (d.item_group or "").lower()
    if any(w in grp for w in BEVERAGE_GROUP_WORDS):
        return True
    nm = (d.item_name or item_code).lower()
    return any(w in nm for w in BEVERAGE_NAME_WORDS)


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
            "takeaway": int(i.get("takeaway") or 0),
            "beverage": 1 if _is_beverage_item(i.item) else 0,
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
            "takeaway": int(i.get("takeaway") or 0),
            "beverage": 1 if _is_beverage_item(i.item) else 0,
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
            "notes": i.get("notes") or "",
            "beverage": 1 if _is_beverage_item(i.item) else 0,
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

    sched_line = ""
    if frappe.db.has_column("POS Order", "scheduled_time") and order.get("scheduled_time"):
        sched_line = ("⏰ *PRE-ORDER for "
                      + frappe.utils.format_datetime(order.scheduled_time, "d MMM h:mm a")
                      + "*\n")

    message = (
        f"*New Order - {order.name}*\n"
        f"{sched_line}"
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


def send_customer_whatsapp(order_name, mobile, dial="94"):
    """Send tracking link to customer via WhatsApp. `dial` = the customer's country dial code
    (default Sri Lanka 94); the number is normalised to international E.164 for the Cloud API."""
    to = _intl_number(mobile, dial)
    if not to or not frappe.db.exists("WhatsApp Account", {}):
        return
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return

    track_url = f"https://luuvgrand.com/cus?phone={mobile}"
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
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message, "preview_url": True}}
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            frappe.log_error(f"Tracking link sent to {to} for {order_name}", "WhatsApp")
    except Exception as e:
        frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")


def process_wa_reply(doc, method):
    """Auto-reply to incoming WhatsApp messages."""
    if doc.type != "Incoming":
        return

    mobile = doc.from_field if hasattr(doc, "from_field") else doc.get("from")
    msg_text = (doc.message or "").strip().upper()

    # Sticker QR → wa.me prefilled message ("… Code: LFXXXXXX") → claim the promo
    # against the SENDER's number (self-verifying) and auto-reply the app link.
    import re
    pm = re.search(r"\bLF[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}\b", msg_text)
    if pm and frappe.db.exists("DocType", "QR Promo Code") \
            and frappe.db.exists("QR Promo Code", pm.group(0)):
        code = pm.group(0)
        local = _local_number(mobile)
        res = _claim_promo_code(code, local)
        if res.get("ok") or res.get("already_mine"):
            item = res.get("item_name") or "meal"
            lead_note = (f"\n⏰ Free-meal orders are for within {PROMO_WINDOW_HOURS} hours."
                         f"\n📍 Pickup only — collect at our {PROMO_PICKUP_OUTLET} outlet (no delivery).")
            if frappe.db.exists("Customer Account", {"mobile": local}):
                # already registered — nothing to fill, straight to ordering
                _send_wa_reply(mobile,
                    f"🎁 Your FREE {item} is unlocked!\n\n"
                    f"You're all set — order here:\nhttps://luuvgrand.com/cus?phone={local}\n\n"
                    f"Your free {item} is added to your FIRST order automatically."
                    f"{lead_note}\n\nSee you soon! 🍔")
            else:
                # send the registration details form (number is proven by this message)
                _send_wa_reply(mobile,
                    f"🎁 Your FREE {item} is reserved!\n\n"
                    f"Save your details here to lock it in:\n"
                    f"https://luuvgrand.com/claim?phone={local}&code={code}\n\n"
                    f"Takes 20 seconds — then your free {item} is added to your "
                    f"first order automatically.{lead_note} 🍔")
        else:
            _send_wa_reply(mobile,
                f"⚠️ {res.get('error') or 'Could not claim this code'}\n\n"
                f"You can still order anytime: https://luuvgrand.com/cus")
        try:
            frappe.log_error("QR Promo claim", f"{code} by {mobile} -> {res}")
        except Exception:
            pass
        return

    # Guide partner self-registration: guide sends "GUIDE" (wa.me deep link on the
    # /guide login screen) → create/refresh their Tour Guide record and auto-reply
    # the app link + a fresh password. The sender's number is proven by the message
    # itself, and the reply rides the 24-hour service window the guide just opened
    # (no Meta template needed). Wrapped so a failure can never break the webhook.
    # Guide partner registration/login over WhatsApp. NEW registrations need a
    # staff-issued invite code (GDXXXXXX) — a bare "guide" in an ordinary
    # customer message must never create a Tour Guide record. Numbers that are
    # already registered just get their magic login link back (self-serve).
    gcode = re.search(GUIDE_CODE_RE, msg_text)
    if (gcode or re.search(r"\bGUIDE\b", msg_text)) and frappe.db.exists("DocType", "Tour Guide"):
        try:
            number = _normalize_wa_number(mobile)
            gname = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
            if gname:
                # Known partner: fresh magic link — the token IS the session,
                # WhatsApp possession is the auth root.
                token = _get_or_create_guide_token(gname)
                pct = float(frappe.db.get_value("Tour Guide", gname, "cashback_percent")
                            or _guide_default_percent())
                _send_wa_reply(mobile,
                    "🧭 Welcome back to the Luuv Fryxo partner program!\n\n"
                    f"Tap your personal link to see your cashback:\n"
                    f"{GUIDE_APP_URL}?t={token}\n\n"
                    f"You earn {pct:g}% cashback on every bill you bring us. 🍔")
            elif gcode and frappe.db.exists("DocType", "Guide Invite Code"):
                row = frappe.db.get_value("Guide Invite Code",
                    {"code": gcode.group(0)}, ["name", "status"], as_dict=True)
                if row and row.status == "New":
                    default_pct = _guide_default_percent()
                    guide = frappe.get_doc({
                        "doctype": "Tour Guide",
                        "whatsapp": number,
                        "role": "Guide",
                        "cashback_percent": default_pct,
                        "status": "Invited",
                        # random unused hash — login is via the magic link
                        "password_hash": _hash_guide_pw(_gen_guide_password()),
                    })
                    guide.insert(ignore_permissions=True)
                    frappe.db.set_value("Guide Invite Code", row.name,
                        {"status": "Used", "used_by": guide.name, "used_on": now_datetime()})
                    token = _get_or_create_guide_token(guide.name)
                    _send_wa_reply(mobile,
                        "🧭 Welcome to the Luuv Fryxo partner program!\n\n"
                        f"You earn {default_pct:g}% cashback on every "
                        f"customer bill you bring us.\n\n"
                        f"Tap your personal link to finish registering — just add "
                        f"your name and details:\n{GUIDE_APP_URL}?t={token}\n\n"
                        "See you soon! 🍔")
                else:
                    _send_wa_reply(mobile,
                        "⚠️ That partner code has already been used or isn't valid.\n\n"
                        "Please ask our staff for a new partner code. 🙏")
            else:
                _send_wa_reply(mobile,
                    "🧭 Luuv Fryxo partner program — earn cashback on every "
                    "customer bill you bring us!\n\n"
                    "To join, ask our staff for your personal partner code, then "
                    "send it here (it looks like GD3XK7MP).")
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Guide WA register")
        return

    # App order confirmation: "Confirming my order #POS-XXXXX" from the customer.
    # Only the number that placed the order can confirm it; then it enters the kitchen.
    om = re.search(r"POS-\d+", msg_text)
    if om and frappe.db.exists("POS Order", om.group(0)):
        oname = om.group(0)
        local = _local_number(mobile)
        row = frappe.db.get_value("POS Order", oname,
                                  ["whatsapp_status", "mobile", "grand_total"], as_dict=True)
        if row and row.whatsapp_status == "Awaiting Confirmation":
            if (row.mobile or "") != local:
                _send_wa_reply(mobile,
                    "⚠️ We couldn't match this order to your number. Please confirm "
                    "from the phone that placed the order.")
                return
            frappe.db.set_value("POS Order", oname,
                                {"whatsapp_status": "Confirmed", "kitchen_status": "Pending"})
            frappe.db.commit()
            sched_txt = ""
            if frappe.db.has_column("POS Order", "scheduled_time"):
                st = frappe.db.get_value("POS Order", oname, "scheduled_time")
                if st:
                    sched_txt = ("\n⏰ Scheduled for "
                                 + frappe.utils.format_datetime(st, "d MMM h:mm a"))
            try:
                if row.grand_total:
                    _process_cashback(local, oname, float(row.grand_total or 0))
            except Exception:
                pass
            try:
                send_order_whatsapp(oname)
            except Exception as e:
                frappe.log_error("Order WA notify", str(e))
            _send_wa_reply(mobile,
                f"✅ Order {oname} confirmed!{sched_txt}\n\n"
                f"Track it: https://luuvgrand.com/cus?phone={local}\n\nThank you! 🍔")
            return
        if row and row.whatsapp_status in ("Confirmed", "Sent"):
            _send_wa_reply(mobile,
                f"👍 Order {oname} is already confirmed.\n"
                f"Track it: https://luuvgrand.com/cus?phone={_local_number(mobile)}")
            return

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
    """Send a text reply via WhatsApp. Returns True when the API accepted it."""
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return False
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
        resp = requests.post(url, headers=headers, json=data)
        return resp.status_code == 200
    except Exception as e:
        frappe.log_error(f"WA reply error: {str(e)}", "WhatsApp")
        return False


# ─── POS Cash Movements (drawer pay-in / pay-out) ────────
# ERPNext has no native mid-shift cash in/out; the Frappe-default way to persist a new
# record type is a DocType. POS Cash Movement is a per-shift drawer log (not GL-integrated).

def _open_shift_name(user=None):
    """The user's own open shift, else the single open shift (so a manager can act on the till)."""
    return frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": user or frappe.session.user}, "name") \
        or frappe.db.get_value("POS Opening Entry", {"docstatus": 1, "status": "Open"}, "name")


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

    opening = _open_shift_name()
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
    opening = _open_shift_name()
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
    if not existing and frappe.db.exists("Item", item_name):
        # Same item_code but a different display name — reuse it, a duplicate
        # item_code would make insert() blow up anyway.
        existing = frappe.db.get_value("Item", item_name,
                                       ["name", "stock_uom"], as_dict=True)
    if existing:
        price = frappe.db.get_value("Item Price",
            {"item_code": existing.name, "price_list": _buying_price_list()},
            "price_list_rate") or 0
        return {"ok": True, "item": existing.name, "name": item_name,
                "unit": existing.stock_uom or unit, "price": float(price), "existed": True}

    try:
        if not frappe.db.exists("UOM", unit):
            frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert(ignore_permissions=True)
        if frappe.db.exists("Item Group", "Raw Materials"):
            group = "Raw Materials"
        else:
            parent = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"
            try:
                frappe.get_doc({"doctype": "Item Group", "item_group_name": "Raw Materials",
                                "parent_item_group": parent, "is_group": 0}).insert(ignore_permissions=True)
                group = "Raw Materials"
            except Exception:
                group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
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
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "purchases: create_purchase_item")
        return {"ok": False, "message": frappe.utils.strip_html(str(e))[:200] or "Could not create item"}
    return {"ok": True, "item": doc.name, "name": doc.item_name,
            "unit": doc.stock_uom, "price": 0}


def _bill_dict(name):
    doc = frappe.get_doc("Purchase Bill", name)
    lines = [{"item": i.item, "name": i.item_name or i.item, "unit": i.uom or "Nos",
              "qty": float(i.qty or 0), "price": float(i.price or 0),
              "old_price": float(i.old_price or 0)} for i in (doc.get("items") or [])]
    total = sum(l["qty"] * l["price"] for l in lines)
    # A bill counts as "due" (owed to the vendor) once it is approved but not yet
    # marked paid. payment_status is a custom field added by setup_purchase_payment_fields;
    # .get() keeps this safe if the column hasn't been created yet (reads as None → unpaid).
    pay_status = doc.get("payment_status") or "unpaid"
    return {
        "name": doc.name, "vendor": doc.vendor or "", "note": doc.note or "",
        "receipt_image": doc.receipt_image or "", "status": doc.status or "pending",
        "by": frappe.get_value("User", doc.requested_by, "full_name") or doc.requested_by or "",
        "time": frappe.utils.pretty_date(doc.requested_at) if doc.requested_at else "",
        "lines": lines, "total": total,
        "payment_status": pay_status,
        "due": (doc.status == "approved" and pay_status != "paid"),
        "paid_at": str(doc.get("paid_at")) if doc.get("paid_at") else "",
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
    # The Due badge has to count every approved-unpaid bill, not just the 60 newest
    # on this page — an older bill still owed was silently missing from the count.
    # One aggregate row straight from the DB: no documents built, nothing to page
    # through, and it uses the same filter get_due_bills() lists by so the two agree.
    if frappe.db.has_column("Purchase Bill", "payment_status"):
        agg = frappe.db.sql("""
            SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS amt
            FROM `tabPurchase Bill`
            WHERE status = 'approved' AND IFNULL(payment_status, 'unpaid') != 'paid'
        """, as_dict=True)[0]
        due_count = int(agg.n or 0)
        due_amt = float(agg.amt or 0)
    else:
        due_amt = sum(b["total"] for b in bills if b.get("due"))
        due_count = sum(1 for b in bills if b.get("due"))
    # Approvals badge: a count, nothing more (the cards only travel when the tab opens).
    appr_count = 0
    if frappe.db.exists("DocType", "POS Approval"):
        appr_count = frappe.db.count("POS Approval",
                                     {"status": "pending", "action": "purchase_bill"})
    return {"bills": bills, "approved_today": approved_today, "pending_amt": pending_amt,
            "week": week, "due_amt": due_amt, "due_count": due_count,
            "appr_count": appr_count}


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


def _block_self_approval(requested_by):
    """Separation of duties: whoever raised a purchase bill cannot be the one who
    approves or rejects it. Approving rewrites the buying price book, so a single
    person must not be able to both enter a purchase and wave it through. No role
    is exempt — a manager who enters a bill still needs a second pair of eyes."""
    if requested_by and requested_by == frappe.session.user:
        frappe.throw(_("You entered this bill — someone else has to approve it"),
                     frappe.PermissionError)


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
    _block_self_approval(frappe.db.get_value("Purchase Bill", name, "requested_by"))
    updated = _decide_purchase_bill(name, decision)
    return {"ok": True, "status": decision, "prices_updated": updated}


# ─── Due bills / pending vendor payments ───────────────────────────
# An approved bill is money owed to the vendor until someone marks it paid. These
# two custom fields turn Purchase Bill into a lightweight payables list; there is
# no GL/accounting behind it (consistent with the rest of this app).

def setup_purchase_payment_fields():
    """Add `payment_status` + `paid_at` + `paid_by` to Purchase Bill. Idempotent; no migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"created": False, "no_doctype": True}
    created = []
    if not frappe.db.has_column("Purchase Bill", "payment_status"):
        create_custom_field("Purchase Bill", {
            "fieldname": "payment_status", "label": "Payment Status", "fieldtype": "Select",
            "options": "unpaid\npaid", "default": "unpaid", "insert_after": "status"})
        created.append("payment_status")
    if not frappe.db.has_column("Purchase Bill", "paid_at"):
        create_custom_field("Purchase Bill", {
            "fieldname": "paid_at", "label": "Paid At", "fieldtype": "Datetime",
            "insert_after": "payment_status"})
        created.append("paid_at")
    if not frappe.db.has_column("Purchase Bill", "paid_by"):
        create_custom_field("Purchase Bill", {
            "fieldname": "paid_by", "label": "Paid By", "fieldtype": "Link",
            "options": "User", "insert_after": "paid_at"})
        created.append("paid_by")
    frappe.clear_cache()
    return {"created": created}


@frappe.whitelist()
def get_due_bills():
    """Approved bills not yet marked paid — the vendor payables list. Oldest owed
    first (so the most overdue payment is at the top), with a per-vendor summary."""
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"bills": [], "total_due": 0, "count": 0, "vendors": []}
    if not frappe.db.has_column("Purchase Bill", "payment_status"):
        setup_purchase_payment_fields()

    # IFNULL, not a plain `!= 'paid'`: a bill created before the payment_status field
    # existed reads NULL, and SQL drops NULL rows from a `!=` comparison — it would be
    # owed money missing from the list. Same predicate the Due badge counts by.
    rows = frappe.db.sql("""
        SELECT name FROM `tabPurchase Bill`
        WHERE status = 'approved' AND IFNULL(payment_status, 'unpaid') != 'paid'
        ORDER BY requested_at ASC LIMIT 200
    """, as_dict=True)
    bills = [_bill_dict(r.name) for r in rows]

    vend = {}
    for b in bills:
        vend[b["vendor"]] = vend.get(b["vendor"], 0) + b["total"]
    vendors = [{"vendor": v or "—", "amount": a}
               for v, a in sorted(vend.items(), key=lambda x: -x[1])]

    return {"bills": bills, "total_due": sum(b["total"] for b in bills),
            "count": len(bills), "vendors": vendors}


@frappe.whitelist(methods=["POST"])
def mark_bill_paid():
    """Mark an approved bill as paid to the vendor (clears it from the due list).
    `paid` param 0/1 lets a mistaken tap be undone."""
    _require_pos_role()
    if not frappe.db.has_column("Purchase Bill", "payment_status"):
        setup_purchase_payment_fields()
    data = frappe.local.form_dict
    name = data.get("name")
    if not name or not frappe.db.exists("Purchase Bill", name):
        frappe.throw(_("Bill not found"))
    if frappe.db.get_value("Purchase Bill", name, "status") != "approved":
        frappe.throw(_("Only an approved bill can be marked paid"))
    paid = str(data.get("paid", "1")) not in ("0", "false", "False", "")
    doc = frappe.get_doc("Purchase Bill", name)
    doc.db_set("payment_status", "paid" if paid else "unpaid")
    doc.db_set("paid_at", now_datetime() if paid else None)
    doc.db_set("paid_by", frappe.session.user if paid else None)
    return {"ok": True, "name": name, "payment_status": "paid" if paid else "unpaid"}


# ─── Expenses (utility bills & other outgoings) ────────────────────
# A CEB bill, gas, rent. Deliberately NOT a Purchase Bill — no items, no qty × price, nothing to
# feed the price book. Like the rest of this app it is not GL-integrated, and by design it does not
# touch the cash drawer either (see create_expense) — it is a record with a manager sign-off.

EXPENSE_CATEGORIES = [
    "Electricity", "Water", "Gas (LP)", "Rent", "Internet/Phone", "Salaries/Wages",
    "Repairs & Maintenance", "Transport/Fuel", "Cleaning & Consumables", "Licences/Fees", "Other",
]


def _ensure_select_option(doctype, fieldname, option):
    """Append an option to a Select on an existing custom DocType — Frappe rejects a value that
    isn't in `options`, so a new approval_type has to be registered before it can be used."""
    dt = frappe.get_doc("DocType", doctype)
    for f in dt.fields:
        if f.fieldname != fieldname:
            continue
        opts = [o for o in (f.options or "").split("\n") if o]
        if option in opts:
            return False
        f.options = "\n".join(opts + [option])
        dt.flags.ignore_permissions = True
        dt.save()
        return True
    return False


@frappe.whitelist()
def setup_expenses():
    """Create the Expense Category + Expense DocTypes, seed categories, and register the
    'expense' approval type. Idempotent (custom:1 => survives bench migrate, never on disk)."""
    created = []

    if not frappe.db.exists("DocType", "Expense Category"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Expense Category", "module": "Zeloura", "custom": 1,
            "naming_rule": "By fieldname", "autoname": "field:category_name",
            "allow_rename": 1, "track_changes": 1,
            "fields": [
                {"fieldname": "category_name", "label": "Category", "fieldtype": "Data",
                 "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "disabled", "label": "Disabled", "fieldtype": "Check",
                 "default": "0", "in_list_view": 1},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1},
            ],
        }).insert(ignore_permissions=True)
        created.append("Expense Category")

    # Seed the defaults, but never re-create one the manager has since deleted: only seed when
    # the table is completely empty.
    if not frappe.db.count("Expense Category"):
        for c in EXPENSE_CATEGORIES:
            frappe.get_doc({"doctype": "Expense Category", "category_name": c}).insert(
                ignore_permissions=True)

    if not frappe.db.exists("DocType", "Expense"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Expense", "module": "Zeloura", "custom": 1,
            "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
            "fields": [
                {"fieldname": "category", "label": "Category", "fieldtype": "Link",
                 "options": "Expense Category", "reqd": 1, "in_list_view": 1},
                {"fieldname": "payee", "label": "Paid To", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1,
                 "in_list_view": 1},
                {"fieldname": "expense_date", "label": "Date", "fieldtype": "Date",
                 "in_list_view": 1},
                {"fieldname": "bill_ref", "label": "Bill / Account No", "fieldtype": "Data"},
                {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
                {"fieldname": "receipt_image", "label": "Receipt", "fieldtype": "Attach Image"},
                {"fieldname": "pos_profile", "label": "Outlet", "fieldtype": "Link",
                 "options": "POS Profile"},
                {"fieldname": "pos_opening_entry", "label": "Shift", "fieldtype": "Link",
                 "options": "POS Opening Entry"},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select",
                 "options": "pending\napproved\nrejected", "default": "pending",
                 "in_list_view": 1},
                {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link",
                 "options": "User"},
                {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
                {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link",
                 "options": "User"},
                {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1, "write": 1, "create": 1},
            ],
        }).insert(ignore_permissions=True)
        created.append("Expense")

    if not frappe.db.exists("DocType", "POS Approval"):
        setup_pos_approval()
    _ensure_select_option("POS Approval", "approval_type", "expense")

    frappe.clear_cache()
    return {"created": created}


@frappe.whitelist()
def get_expense_categories():
    """Categories for the /purchases expense form. Manager-editable from Desk > Expense Category."""
    if not frappe.db.exists("DocType", "Expense Category"):
        setup_expenses()
    return {"categories": [r.name for r in frappe.get_all(
        "Expense Category", filters={"disabled": 0}, fields=["name"], order_by="name")]}


def _expense_dict(name):
    d = frappe.get_doc("Expense", name)
    return {
        "name": d.name, "category": d.category, "payee": d.payee or "",
        "amount": float(d.amount or 0), "date": str(d.expense_date or ""),
        "bill_ref": d.bill_ref or "", "note": d.note or "",
        "receipt_image": d.receipt_image or "", "outlet": d.pos_profile or "",
        "status": d.status, "shift": d.pos_opening_entry or "",
        "by": frappe.get_value("User", d.requested_by, "full_name") or d.requested_by or "",
        "when": frappe.utils.pretty_date(d.requested_at) if d.requested_at else "",
    }


@frappe.whitelist(methods=["POST"])
def create_expense():
    """Record an expense and route it to the manager for approval.

    Deliberately does NOT write a POS Cash Movement. The drawer's expected cash is computed in two
    places that must agree — pos2's close dialog (client-side) and ERPNext's expected_amount — and
    neither accounts for pay-outs. Subtracting an expense in only one of them is worse than in
    neither, so an expense is a record, not a till movement. The shift is stored for context only.
    """
    _require_pos_role()
    if not frappe.db.exists("DocType", "Expense"):
        setup_expenses()
    data = frappe.local.form_dict

    category = (data.get("category") or "").strip()
    if not category or not frappe.db.exists("Expense Category", category):
        frappe.throw(_("Pick a category"))
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero"))
    payee = (data.get("payee") or "").strip()
    note = (data.get("note") or "").strip()

    # A utility bill can be recorded with no shift open, so neither is required — they just record
    # which outlet/shift it was raised against when there is one.
    opening = _open_shift_name()
    pos_profile = (frappe.db.get_value("POS Opening Entry", opening, "pos_profile")
                   if opening else _resolve_pos_profile_name())

    doc = frappe.get_doc({
        "doctype": "Expense", "category": category, "payee": payee, "amount": amount,
        "expense_date": data.get("expense_date") or frappe.utils.nowdate(),
        "bill_ref": (data.get("bill_ref") or "").strip(), "note": note,
        "receipt_image": data.get("receipt_image") or "",
        "pos_profile": pos_profile, "pos_opening_entry": opening or "", "status": "pending",
        "requested_by": frappe.session.user, "requested_at": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert()

    if not frappe.db.exists("DocType", "POS Approval"):
        setup_pos_approval()
    amt_str = f"Rs {int(round(amount)):,}"
    lines = [{"qtyStr": "", "name": payee or category, "totalStr": amt_str}]
    if data.get("bill_ref"):
        lines.append({"qtyStr": "", "name": "Bill no", "totalStr": data.get("bill_ref")})
    frappe.get_doc({
        "doctype": "POS Approval", "approval_type": "expense", "title": category,
        "subhead": (payee + " · " if payee else "") + "expense",
        "details_json": json.dumps({"lines": lines, "footer_label": "Amount",
                                    "footer_value": amt_str}),
        "flag_text": note or "Expense — approving records it against the outlet",
        "action": "expense", "action_payload": json.dumps({"expense": doc.name}),
        "status": "pending", "requested_by": frappe.session.user, "requested_at": now_datetime(),
    }).insert(ignore_permissions=True)
    return {"ok": True, "name": doc.name, "amount": amount}


def _decide_expense(name, decision):
    """Approve/reject an expense. Nothing to execute beyond the status — an expense has no side
    effect to undo (no price book, no drawer movement, no GL)."""
    doc = frappe.get_doc("Expense", name)
    if doc.status != "pending":
        return False
    doc.db_set("status", decision)
    doc.db_set("resolved_by", frappe.session.user)
    doc.db_set("resolved_at", now_datetime())
    return True


@frappe.whitelist()
def get_expenses():
    """Recent expenses + header stats for the /purchases app."""
    if not frappe.db.exists("DocType", "Expense"):
        return {"expenses": [], "approved_today": 0, "pending_amt": 0, "week": 0}
    rows = frappe.get_all("Expense", fields=["name"], order_by="requested_at desc",
                          limit_page_length=60)
    expenses = [_expense_dict(r.name) for r in rows]
    today = frappe.utils.today()
    week_start = frappe.utils.add_days(today, -6)
    approved_today = sum(e["amount"] for e in expenses
                         if e["status"] == "approved" and e["date"] == today)
    pending_amt = sum(e["amount"] for e in expenses if e["status"] == "pending")
    week = sum(e["amount"] for e in expenses
               if e["status"] != "rejected" and e["date"] >= week_start)
    return {"expenses": expenses, "approved_today": approved_today,
            "pending_amt": pending_amt, "week": week}


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

    # ── revenue breakdown: service charge vs other charges vs net sales ──
    # Service charge is posted as a Sales Taxes and Charges row (description
    # "Service Charge", account "Service - LF") by _append_service_charge_tax,
    # NOT a line item. Bucket the range's tax rows by that description; net sales
    # is the remainder so the three segments always sum to Total revenue.
    tax = frappe.db.sql(
        """
        SELECT
          COALESCE(SUM(CASE WHEN t.description = 'Service Charge' THEN t.tax_amount ELSE 0 END), 0) AS sc,
          COALESCE(SUM(CASE WHEN t.description <> 'Service Charge' THEN t.tax_amount ELSE 0 END), 0) AS other
        FROM `tabSales Taxes and Charges` t
        INNER JOIN `tabPOS Invoice` pi ON pi.name = t.parent AND pi.docstatus = 1
        WHERE t.parenttype = 'POS Invoice' AND pi.posting_date BETWEEN %s AND %s
        """,
        (start, end), as_dict=True)[0]
    service_charge = float(tax.sc or 0)
    other_charges = float(tax.other or 0)
    net_sales = total_revenue - service_charge - other_charges

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
        "revenue_breakdown": {
            "net_sales": net_sales,
            "service_charge": service_charge,
            "other_charges": other_charges,
            "total": total_revenue,
        },
    }


@frappe.whitelist()
def get_manager_day_report(date=None):
    """Full breakdown of a single day — powers the drill-down when a manager
    taps a bar on the Reports daily chart (payment mix, hourly, item-wise)."""
    from frappe.utils import getdate, add_days, formatdate, get_datetime, today as _today

    day = getdate(date or _today())
    day_start = str(day)
    day_end = str(add_days(day, 1))

    invoices = frappe.get_all("POS Invoice",
        filters={"docstatus": 1, "posting_date": day},
        fields=["name", "grand_total", "creation"])
    revenue = sum(float(i.grand_total or 0) for i in invoices)
    sales_count = len(invoices)
    avg = revenue / sales_count if sales_count else 0

    # payment mix + hourly revenue + the day's bill list (newest first)
    pay = {}
    hourly = {}
    bills = []
    for inv in invoices:
        doc = frappe.get_doc("POS Invoice", inv.name)
        modes = []
        for p in (doc.payments or []):
            mode = p.mode_of_payment or "Other"
            amt = float(p.amount or 0)
            pay[mode] = pay.get(mode, 0) + amt
            if amt and mode not in modes:
                modes.append(mode)
        created = get_datetime(inv.creation)
        h = created.hour
        hourly[h] = hourly.get(h, 0) + float(inv.grand_total or 0)
        bills.append({
            "name": inv.name,
            "no": inv.name.split("-")[-1],
            "time": created.strftime("%I:%M %p").lstrip("0"),
            "creation": str(inv.creation),
            "total": float(inv.grand_total or 0),
            "modes": modes,
        })
    bills.sort(key=lambda b: b["creation"], reverse=True)
    payments = [{"mode": m, "amount": a} for m, a in sorted(pay.items(), key=lambda x: -x[1])]
    hourly_list = [{"hour": h, "amount": hourly[h]} for h in sorted(hourly.keys())]

    # item-wise sales for the day (from paid POS Orders)
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
        (day_start, day_end), as_dict=True)
    items = [{"name": r.name, "qty": int(r.qty or 0), "revenue": float(r.revenue or 0)}
             for r in item_rows if r.name]

    # voids that day
    voids = frappe.get_all("POS Order",
        filters=[["docstatus", "=", 2], ["kitchen_status", "=", "Cancelled"],
                 ["modified", ">=", day_start], ["modified", "<", day_end]],
        fields=["grand_total"])

    return {
        "date": day_start,
        "label": formatdate(day, "EEE d MMM"),
        "revenue": revenue,
        "sales_count": sales_count,
        "avg_ticket": avg,
        "void_count": len(voids),
        "void_amount": sum(float(v.grand_total or 0) for v in voids),
        "payments": payments,
        "hourly": hourly_list,
        "items": items,
        "bills": bills,
    }


@frappe.whitelist()
def get_manager_monthly_sales(months=12):
    """Month-by-month revenue for the Manager Reports tab (last N calendar months,
    zero-filled so quiet months still show)."""
    import datetime
    from frappe.utils import getdate, formatdate, today as _today

    try:
        months = int(months)
    except Exception:
        months = 12
    months = max(1, min(months, 24))

    end = getdate(_today())
    start_year, start_month = end.year, end.month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = datetime.date(start_year, start_month, 1)

    rows = frappe.db.sql(
        """
        SELECT DATE_FORMAT(posting_date, '%%Y-%%m') AS ym,
               COALESCE(SUM(grand_total), 0) AS revenue,
               COUNT(*) AS cnt
        FROM `tabPOS Invoice`
        WHERE docstatus = 1 AND posting_date >= %s
        GROUP BY ym
        """,
        (start,), as_dict=True)
    by_ym = {r.ym: r for r in rows}

    out = []
    y, mo = start_year, start_month
    for _ in range(months):
        ym = "%04d-%02d" % (y, mo)
        r = by_ym.get(ym)
        revenue = float(r.revenue or 0) if r else 0
        cnt = int(r.cnt or 0) if r else 0
        out.append({"ym": ym, "label": formatdate(datetime.date(y, mo, 1), "MMM"),
                    "year": y, "yy": str(y)[2:], "revenue": revenue, "count": cnt,
                    "avg_ticket": (revenue / cnt) if cnt else 0})
        mo += 1
        if mo == 13:
            mo = 1
            y += 1

    best = max(out, key=lambda m: m["revenue"]) if out else None
    return {"months": out, "best": best,
            "total_revenue": sum(m["revenue"] for m in out),
            "total_sales": sum(m["count"] for m in out)}


@frappe.whitelist()
def get_manager_bill_detail(name=None):
    """Everything on one POS Invoice — powers the manager's tap-a-bill drill-down
    (items, discount, taxes/SC, payments, cashier, table)."""
    from frappe.utils import get_datetime, formatdate

    if not name or not frappe.db.exists("POS Invoice", name):
        frappe.throw(_("Bill not found"))
    doc = frappe.get_doc("POS Invoice", name)

    items = [{"name": i.item_name or i.item_code, "qty": float(i.qty or 0),
              "rate": float(i.rate or 0), "amount": float(i.amount or 0)}
             for i in (doc.items or [])]
    taxes = [{"label": (t.description or t.account_head or "Charge"),
              "amount": float(t.tax_amount or 0)}
             for t in (doc.taxes or []) if float(t.tax_amount or 0)]
    payments = [{"mode": p.mode_of_payment or "Other", "amount": float(p.amount or 0)}
                for p in (doc.payments or []) if float(p.amount or 0)]

    order = frappe.db.get_value("POS Order", {"pos_invoice": name},
        ["name", "restaurant_table", "order_source", "customer_name", "waiter_name"],
        as_dict=True) or {}
    table = (order.get("restaurant_table") or "").split("-")[-1] if order.get("restaurant_table") else ""

    created = get_datetime(doc.creation)
    return {
        "name": doc.name,
        "no": doc.name.split("-")[-1],
        "date_label": formatdate(doc.posting_date, "EEE d MMM"),
        "time": created.strftime("%I:%M %p").lstrip("0"),
        "cashier": frappe.get_value("User", doc.owner, "full_name") or doc.owner,
        "customer": order.get("customer_name") or doc.customer_name or "",
        "table": table,
        "source": order.get("order_source") or "",
        "items": items,
        "net_total": float(doc.net_total or 0),
        "discount": float(doc.discount_amount or 0),
        "taxes": taxes,
        "grand_total": float(doc.grand_total or 0),
        "payments": payments,
        "is_return": bool(doc.is_return),
    }


@frappe.whitelist()
def get_manager_purchase_report(days=7):
    """Daily + weekly purchasing spend over the last N days (approved bills only).

    Powers the Manager app 'Purchases' tab: a day-by-day and week-by-week spend
    chart plus top-vendor and top-item breakdowns. Spend is summed from the bill
    line items (qty * price) to match how the /purchases app recomputes totals.
    Purchase Bill is non-submittable, so we filter on status='approved' (committed
    spend), not docstatus; the effective purchase date is `requested_at`.
    """
    from frappe.utils import getdate, add_days, formatdate, today as _today

    try:
        days = int(days)
    except Exception:
        days = 7
    days = max(1, min(days, 92))  # cap the window

    end = getdate(_today())
    start = add_days(end, -(days - 1))
    # requested_at is a Datetime; upper bound is exclusive start-of-next-day.
    range_start = str(start)
    range_end = str(add_days(end, 1))

    # ── daily spend + bill count (one grouped query over the line items) ──
    rows = frappe.db.sql(
        """
        SELECT DATE(pb.requested_at) AS d,
               COALESCE(SUM(pbi.qty * pbi.price), 0) AS spend,
               COUNT(DISTINCT pb.name) AS cnt
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        GROUP BY DATE(pb.requested_at)
        """,
        (range_start, range_end), as_dict=True)
    by_date = {str(r.d): {"spend": float(r.spend or 0), "count": int(r.cnt or 0)} for r in rows}

    daily = []
    cur = start
    while cur <= end:
        key = str(cur)
        rec = by_date.get(key, {"spend": 0, "count": 0})
        daily.append({
            "date": key,
            "label": formatdate(cur, "d MMM"),
            "weekday": formatdate(cur, "EEE"),
            "spend": rec["spend"],
            "count": rec["count"],
        })
        cur = add_days(cur, 1)

    # ── weekly spend (ISO weeks, Monday-anchored) ──
    wrows = frappe.db.sql(
        """
        SELECT YEARWEEK(pb.requested_at, 3) AS yw,
               DATE(DATE_SUB(pb.requested_at, INTERVAL WEEKDAY(pb.requested_at) DAY)) AS wk_start,
               COALESCE(SUM(pbi.qty * pbi.price), 0) AS spend,
               COUNT(DISTINCT pb.name) AS cnt
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        GROUP BY yw, wk_start
        ORDER BY wk_start
        """,
        (range_start, range_end), as_dict=True)
    weekly = [{
        "week_start": str(r.wk_start),
        "label": formatdate(getdate(r.wk_start), "d MMM"),
        "weekday": "Wk",
        "spend": float(r.spend or 0),
        "count": int(r.cnt or 0),
    } for r in wrows]

    # ── top vendors ──
    vrows = frappe.db.sql(
        """
        SELECT pb.vendor AS name,
               COALESCE(SUM(pbi.qty * pbi.price), 0) AS spend,
               COUNT(DISTINCT pb.name) AS cnt
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        GROUP BY pb.vendor
        ORDER BY spend DESC
        """,
        (range_start, range_end), as_dict=True)
    vendors = [{"name": r.name or "—", "spend": float(r.spend or 0), "count": int(r.cnt or 0)}
               for r in vrows]

    # ── top items bought ──
    irows = frappe.db.sql(
        """
        SELECT COALESCE(pbi.item_name, pbi.item) AS name,
               SUM(pbi.qty) AS qty,
               COALESCE(SUM(pbi.qty * pbi.price), 0) AS spend
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        GROUP BY name
        ORDER BY spend DESC
        """,
        (range_start, range_end), as_dict=True)
    items = [{"name": r.name, "qty": float(r.qty or 0), "spend": float(r.spend or 0)}
             for r in irows if r.name]

    total_spend = sum(d["spend"] for d in daily)
    bill_count = sum(d["count"] for d in daily)
    avg_bill = total_spend / bill_count if bill_count else 0
    best_day = max(daily, key=lambda d: d["spend"]) if daily else None

    return {
        "days": days,
        "start": str(start),
        "end": str(end),
        "daily": daily,
        "weekly": weekly,
        "vendors": vendors,
        "vendors_spend": sum(v["spend"] for v in vendors) or 1,
        "items": items,
        "items_spend": sum(i["spend"] for i in items) or 1,
        "total_spend": total_spend,
        "bill_count": bill_count,
        "avg_bill": avg_bill,
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


def _pending_and_recent_approvals(action=None, include_recent=True):
    """Everything still pending, plus whatever was resolved today. `action` narrows
    it to one kind (the purchases page wants purchase bills only) — filtered in the
    query, not afterwards, so a shop with hundreds of pending approvals does not ship
    them all to Python to throw most away. `include_recent=False` drops the resolved
    tail entirely, for a list that is meant to be a work queue and nothing else."""
    if not frappe.db.exists("DocType", "POS Approval"):
        return []
    fields = ["name", "approval_type", "pos_order", "title", "subhead", "details_json",
              "flag_text", "status", "requested_by", "requested_at", "resolved_at",
              "action", "action_payload"]
    if frappe.db.has_column("POS Approval", "manager_comment"):
        fields.append("manager_comment")
    pending_filters = {"status": "pending"}
    recent_filters = [["status", "in", ["approved", "rejected"]],
                      ["modified", ">=", frappe.utils.today()]]
    if action:
        pending_filters["action"] = action
        recent_filters.append(["action", "=", action])
    pending = frappe.get_all("POS Approval", filters=pending_filters,
                             fields=fields, order_by="requested_at desc")
    if not include_recent:
        return pending
    recent = frappe.get_all("POS Approval", filters=recent_filters,
                            fields=fields, order_by="modified desc", limit_page_length=15)
    return pending + recent


def _format_approvals(rows):
    out = []
    # One lookup per distinct user, not per row: the manager polls this every 8s
    # with every pending approval in hand.
    names = {}
    for r in rows:
        u = r.get("requested_by")
        if u and u not in names:
            names[u] = frappe.get_value("User", u, "full_name") or u
    me = frappe.session.user
    for r in rows:
        try:
            details = json.loads(r.get("details_json") or "{}")
        except Exception:
            details = {}
        # Purchase-bill / expense approvals carry a receipt photo on the linked
        # doc — surface it so the approver can inspect the actual bill.
        image = ""
        try:
            payload = json.loads(r.get("action_payload") or "{}")
            action = r.get("action") or ""
            if action == "purchase_bill" and payload.get("bill"):
                image = frappe.db.get_value("Purchase Bill", payload["bill"], "receipt_image") or ""
            elif action == "expense" and payload.get("expense"):
                image = frappe.db.get_value("Expense", payload["expense"], "receipt_image") or ""
        except Exception:
            image = ""
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
            "image": image,
            "manager_comment": r.get("manager_comment") or "",
            "by": names.get(r.get("requested_by"), ""),
            # Whoever raised it cannot resolve it — the UI greys the buttons out,
            # _block_self_approval() is what actually enforces it.
            "mine": bool(r.get("requested_by")) and r.get("requested_by") == me,
        })
    return out


@frappe.whitelist()
def get_pending_approvals():
    rows = _pending_and_recent_approvals()
    pending = sum(1 for r in rows if r.get("status") == "pending")
    return {"approvals": _format_approvals(rows), "pending": pending}


@frappe.whitelist()
def get_purchase_approvals():
    """Purchase-bill approvals for the purchases page — the same cards the manager
    app shows, with every other approval type left out. Open to any POS user on the
    page, not managers only: whoever is on the floor can clear a colleague's bill,
    while their own stays blocked by _block_self_approval().

    Pending only. This tab is a queue to empty, not a history — a resolved bill
    belongs in the Bills list, where it already shows with its status."""
    _require_pos_role()
    rows = _pending_and_recent_approvals("purchase_bill", include_recent=False)
    return {"approvals": _format_approvals(rows), "pending": len(rows)}


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
    # Purchase bills only: the manager app resolves every kind of approval through
    # here, and a cashier asking for a discount on their own sale is a different
    # thing from entering money paid to a vendor.
    if (doc.action or "") == "purchase_bill":
        _block_self_approval(doc.requested_by)
    doc.db_set("status", decision)
    doc.db_set("resolved_by", frappe.session.user)
    doc.db_set("resolved_at", now_datetime())
    comment = (data.get("comment") or "").strip()
    if comment and frappe.db.has_column("POS Approval", "manager_comment"):
        doc.db_set("manager_comment", comment[:500])

    executed = None
    action = (doc.action or "")
    try:
        if action == "purchase_bill":
            payload = json.loads(doc.action_payload or "{}")
            bill = payload.get("bill")
            if bill and frappe.db.exists("Purchase Bill", bill):
                _decide_purchase_bill(bill, decision)
                executed = "purchase_" + decision
        elif action == "expense":
            # Runs on reject too — that's what pulls the drawer pay-out back out.
            payload = json.loads(doc.action_payload or "{}")
            expense = payload.get("expense")
            if expense and frappe.db.exists("Expense", expense):
                _decide_expense(expense, decision)
                executed = "expense_" + decision
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
    # Preserve per-line notes across the rebuild (callers don't send notes back).
    _ensure_item_notes_field()
    notes_map = {}
    for i in (pos_order.get("items") or []):
        key = (i.item, int(i.get("takeaway") or 0))
        n = _clean_item_note(i.get("notes"))
        if n:
            parts = [p for p in notes_map.get(key, "").split("; ") if p]
            if n not in parts:
                parts.append(n)
            notes_map[key] = "; ".join(parts)
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
        ta_flag = 1 if item_ta else 0
        line_note = _clean_item_note(item_data.get("notes")) or notes_map.get((item_code, ta_flag), "")
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty,
                                   "rate": rate, "takeaway": ta_flag, "notes": line_note})
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


# ─── POS Item Add-ons (extra sauce, salad… offered when a menu item is added) ─

@frappe.whitelist()
def setup_item_addons():
    """Create the `POS Item Addon` mapping DocType (idempotent). One row = one
    add-on offered with a menu item; manage rows from Desk (/app/pos-item-addon).
    pos2 reads enabled rows and pops an add-on picker when the item is added."""
    if frappe.db.exists("DocType", "POS Item Addon"):
        return {"created": False, "exists": True}
    frappe.get_doc({
        "doctype": "DocType", "name": "POS Item Addon", "module": "Zeloura", "custom": 1,
        "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
        "fields": [
            {"fieldname": "item", "label": "Menu Item", "fieldtype": "Link",
             "options": "Item", "reqd": 1, "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "addon_item", "label": "Add-on Item", "fieldtype": "Link",
             "options": "Item", "reqd": 1, "in_list_view": 1},
            {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check",
             "default": "1", "in_list_view": 1},
        ],
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"created": True}


# ─── QR Promo Campaign (sticker QR → register in /cus → free meal on first order) ─
# Flow: unique code per printed sticker → customer scans → wa.me WhatsApp msg →
# bot reply links to /cus (old /app?promo= / /customer?promo= links redirect to /cus?promo=) →
# registers → code Registered against their mobile → their first app order
# auto-adds the campaign's free item at Rs 0 → code Redeemed (single use).

PROMO_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L lookalikes
PROMO_WA_NUMBER = "94724444901"  # WhatsApp business number the sticker QRs open (wa.me)
PROMO_WINDOW_HOURS = 6  # free-item orders must be for a time within this many hours (time optional → ASAP)
PROMO_PICKUP_OUTLET = "Kurunegala"  # free-meal orders are pickup-only, collected at THIS outlet (no delivery)


@frappe.whitelist()
def setup_scheduled_time_field():
    """Custom field: POS Order.scheduled_time — the customer's preferred time
    (free-item orders must be within PROMO_WINDOW_HOURS from now)."""
    created = False
    if not frappe.db.has_column("POS Order", "scheduled_time"):
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        create_custom_field("POS Order", {
            "fieldname": "scheduled_time", "label": "Scheduled Time",
            "fieldtype": "Datetime", "insert_after": "kitchen_status"})
        created = True
    frappe.clear_cache()
    return {"scheduled_time_created": created}


@frappe.whitelist()
def setup_qr_promo():
    """Create QR Promo Campaign + QR Promo Code DocTypes (idempotent; custom:1)."""
    created = []
    if not frappe.db.exists("DocType", "QR Promo Campaign"):
        frappe.get_doc({
            "doctype": "DocType", "name": "QR Promo Campaign", "module": "Zeloura", "custom": 1,
            "naming_rule": "By fieldname", "autoname": "field:campaign_name", "track_changes": 1,
            "fields": [
                {"fieldname": "campaign_name", "label": "Campaign Name", "fieldtype": "Data",
                 "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "free_item", "label": "Free Item", "fieldtype": "Link",
                 "options": "Item", "in_list_view": 1},
                {"fieldname": "active", "label": "Active", "fieldtype": "Check", "default": "1"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert(ignore_permissions=True)
        created.append("QR Promo Campaign")
    if not frappe.db.exists("DocType", "QR Promo Code"):
        frappe.get_doc({
            "doctype": "DocType", "name": "QR Promo Code", "module": "Zeloura", "custom": 1,
            "naming_rule": "By fieldname", "autoname": "field:code", "track_changes": 1,
            "fields": [
                {"fieldname": "code", "label": "Code", "fieldtype": "Data",
                 "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "campaign", "label": "Campaign", "fieldtype": "Link",
                 "options": "QR Promo Campaign", "in_list_view": 1},
                {"fieldname": "batch", "label": "Batch", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select",
                 "options": "New\nRegistered\nRedeemed", "default": "New", "in_list_view": 1},
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data"},
                {"fieldname": "registered_on", "label": "Registered On", "fieldtype": "Datetime"},
                {"fieldname": "redeemed_on", "label": "Redeemed On", "fieldtype": "Datetime"},
                {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link", "options": "POS Order"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert(ignore_permissions=True)
        created.append("QR Promo Code")
    if not frappe.db.exists("QR Promo Campaign", "Free Meal Launch"):
        frappe.get_doc({"doctype": "QR Promo Campaign", "campaign_name": "Free Meal Launch",
                        "active": 1}).insert(ignore_permissions=True)
        created.append("Free Meal Launch campaign")
    frappe.db.commit()
    return {"status": "created" if created else "already_exists", "created": created}


def _active_promo_campaign():
    rows = frappe.get_all("QR Promo Campaign", filters={"active": 1},
                          fields=["name", "free_item"], limit=1)
    return rows[0] if rows else None


def _pending_promo_for(mobile):
    """Registered-but-unredeemed QR promo code for this mobile (or None)."""
    if not mobile or not frappe.db.exists("DocType", "QR Promo Code"):
        return None
    rows = frappe.get_all("QR Promo Code",
        filters={"mobile": mobile, "status": "Registered"},
        fields=["name", "campaign"], limit=1)
    if not rows:
        return None
    free_item = frappe.db.get_value("QR Promo Campaign", rows[0].campaign, "free_item")
    return {"name": rows[0].name, "campaign": rows[0].campaign, "free_item": free_item}


@frappe.whitelist(methods=["POST"])
def promo_set_item():
    """Manager sets/changes the free item on the active campaign."""
    _require_pos_role()
    item = (frappe.local.form_dict.get("item") or "").strip()
    if not item or not frappe.db.exists("Item", item):
        frappe.throw(_("Pick a valid menu item"))
    camp = _active_promo_campaign()
    if not camp:
        setup_qr_promo()
        camp = _active_promo_campaign()
    frappe.db.set_value("QR Promo Campaign", camp["name"], "free_item", item)
    frappe.db.commit()
    return {"ok": True, "campaign": camp["name"], "free_item": item,
            "item_name": frappe.db.get_value("Item", item, "item_name") or item}


@frappe.whitelist(methods=["POST"])
def create_promo_codes():
    """Bulk-generate N unique single-use codes for sticker printing; returns them all."""
    _require_pos_role()
    data = frappe.local.form_dict
    try:
        count = int(data.get("count") or 0)
    except Exception:
        count = 0
    count = max(1, min(count, 1000))
    batch = (data.get("batch") or "").strip() or str(frappe.utils.today())
    camp = _active_promo_campaign()
    if not camp:
        setup_qr_promo()
        camp = _active_promo_campaign()
    codes = []
    while len(codes) < count:
        code = "LF" + "".join(secrets.choice(PROMO_CODE_ALPHABET) for _ in range(6))
        if frappe.db.exists("QR Promo Code", code):
            continue
        frappe.get_doc({"doctype": "QR Promo Code", "code": code, "campaign": camp["name"],
                        "batch": batch, "status": "New"}).insert(ignore_permissions=True)
        codes.append(code)
    frappe.db.commit()
    return {"ok": True, "campaign": camp["name"], "batch": batch, "count": len(codes),
            "codes": codes}


@frappe.whitelist()
def get_promo_overview():
    """Campaign + batch stats + recent codes for the /qrpromo admin page."""
    _require_pos_role()
    if not frappe.db.exists("DocType", "QR Promo Code"):
        return {"setup": False, "campaign": None, "batches": [], "stats": {}}
    camp = _active_promo_campaign()
    item_name = ""
    if camp and camp.get("free_item"):
        item_name = frappe.db.get_value("Item", camp["free_item"], "item_name") or camp["free_item"]
    rows = frappe.db.sql(
        """
        SELECT batch,
               COUNT(*) AS total,
               SUM(status = 'New') AS new_cnt,
               SUM(status = 'Registered') AS registered_cnt,
               SUM(status = 'Redeemed') AS redeemed_cnt,
               MIN(creation) AS created
        FROM `tabQR Promo Code`
        GROUP BY batch ORDER BY MIN(creation) DESC
        """, as_dict=True)
    batches = [{"batch": r.batch or "", "total": int(r.total or 0), "new": int(r.new_cnt or 0),
                "registered": int(r.registered_cnt or 0), "redeemed": int(r.redeemed_cnt or 0),
                "created": str(r.created or "")[:10]} for r in rows]
    stats = {"total": sum(b["total"] for b in batches),
             "registered": sum(b["registered"] for b in batches),
             "redeemed": sum(b["redeemed"] for b in batches)}
    return {"setup": True,
            "campaign": {"name": camp["name"], "free_item": camp.get("free_item") or "",
                         "item_name": item_name} if camp else None,
            "batches": batches, "stats": stats}


@frappe.whitelist()
def get_promo_batch_codes(batch):
    """All codes of one batch (re-download / re-print a printed batch)."""
    _require_pos_role()
    rows = frappe.get_all("QR Promo Code", filters={"batch": batch},
                          fields=["code", "status"], order_by="creation asc")
    return {"batch": batch, "codes": [r.code for r in rows],
            "statuses": {r.code: r.status for r in rows}}


def _claim_promo_code(code, mobile):
    """Core claim: attach a New code to a mobile. No messaging side-effects —
    callers (web promo_register / WhatsApp webhook) send their own replies."""
    if not mobile or len(mobile) < 10 or not code:
        return {"ok": False, "error": "Valid mobile and code required"}
    if not frappe.db.exists("DocType", "QR Promo Code"):
        return {"ok": False, "error": "Promo not available"}
    row = frappe.db.get_value("QR Promo Code", {"code": code},
                              ["name", "status", "campaign", "mobile"], as_dict=True)
    if not row:
        return {"ok": False, "error": "Invalid promo code"}
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    if row.status != "New":
        if (row.mobile or "") == mobile:
            return {"ok": False, "already_mine": True, "item_name": item_name,
                    "error": "You already claimed this code"}
        return {"ok": False, "error": "This QR code was already used"}
    if frappe.db.exists("QR Promo Code",
                        {"mobile": mobile, "status": ["in", ["Registered", "Redeemed"]]}):
        return {"ok": False, "error": "This phone already claimed a free meal"}
    frappe.db.set_value("QR Promo Code", row.name,
                        {"status": "Registered", "mobile": mobile,
                         "registered_on": now_datetime()})
    frappe.db.commit()
    return {"ok": True, "item_name": item_name}


@frappe.whitelist(allow_guest=True)
def promo_register():
    """Attach a scanned QR promo code to a newly registered /app customer."""
    data = frappe.local.form_dict
    mobile = (data.get("mobile") or "").strip()
    code = (data.get("code") or "").strip().upper()
    res = _claim_promo_code(code, mobile)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "Could not claim"}
    item_name = res.get("item_name") or ""

    # WhatsApp confirmation to the customer (best-effort, never blocks the claim)
    wa_sent = False
    try:
        cust = frappe.db.get_value("Customer Account", {"mobile": mobile}, "customer_name") or ""
        what = item_name or "meal"
        msg = (
            f"🎁 *Luuv Fryxo — FREE {what}!*\n\n"
            f"Hi{(' ' + cust) if cust else ''}! Your free {what} is locked in ✅\n\n"
            f"Order anything on our app and your free {what} is added to your FIRST order automatically.\n\n"
            f"👉 Order now: https://luuvgrand.com/cus\n\n"
            f"Offer: one per customer, first app order only."
        )
        wa_sent = bool(_send_wa_reply(_wa_number(mobile), msg))
    except Exception as e:
        frappe.log_error(f"Promo WhatsApp error: {str(e)}", "QR Promo")
    return {"ok": True, "item_name": item_name, "whatsapp_sent": wa_sent}


def _wa_number(mobile):
    """Local 07XXXXXXXX → international 947XXXXXXXX for the WhatsApp Cloud API."""
    m = (mobile or "").strip().replace("+", "").replace(" ", "")
    if m.startswith("0") and len(m) == 10:
        return "94" + m[1:]
    return m


def _intl_number(mobile, dial="94"):
    """National/local number + a country dial code → international E.164 digits (no +) for the
    WhatsApp Cloud API. Strips non-digits and a single leading 0; if the number already begins
    with the dial code it's left as-is. dial defaults to Sri Lanka (94)."""
    m = "".join(ch for ch in (mobile or "") if ch.isdigit())
    d = "".join(ch for ch in (dial or "94") if ch.isdigit()) or "94"
    if not m:
        return ""
    if m.startswith("0"):
        m = m[1:]
    if m.startswith(d):
        return m
    return d + m


def _local_number(mobile):
    """International 947XXXXXXXX (webhook sender) → local 07XXXXXXXX (how Customer
    Account / POS Order mobiles are stored)."""
    m = (mobile or "").strip().replace("+", "").replace(" ", "")
    if m.startswith("94") and len(m) == 11:
        return "0" + m[2:]
    return m


@frappe.whitelist(allow_guest=True)
def promo_check():
    """Validate a scanned promo code for the /app landing banner (no side effects)."""
    code = (frappe.local.form_dict.get("code") or "").strip().upper()
    if not code or not frappe.db.exists("DocType", "QR Promo Code"):
        return {"valid": False, "used": False}
    row = frappe.db.get_value("QR Promo Code", {"code": code}, ["status", "campaign"], as_dict=True)
    if not row:
        return {"valid": False, "used": False}
    if row.status != "New":
        return {"valid": False, "used": True}
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    return {"valid": True, "used": False, "item_name": item_name or "meal"}


def _valid_claim_pair(phone, code):
    """The (phone, code) pair is legit only if that code was claimed BY that phone
    via the WhatsApp webhook — so a /claim link can't be forged for another number."""
    if not phone or not code or not frappe.db.exists("DocType", "QR Promo Code"):
        return None
    row = frappe.db.get_value("QR Promo Code", {"code": code},
                              ["name", "status", "campaign", "mobile"], as_dict=True)
    if not row or (row.mobile or "") != phone or row.status not in ("Registered", "Redeemed"):
        return None
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    return {"item_name": item_name}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def claim_save_details():
    """Save the customer's details from the /claim form (sent via WhatsApp reply).
    The number is already proven — the claim came from their own WhatsApp."""
    data = frappe.local.form_dict
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "").strip()
    pin = (data.get("pin") or "").strip()

    address = (data.get("address") or "").strip()
    area = (data.get("area") or "").strip()

    pair = _valid_claim_pair(phone, code)
    if not pair:
        return {"ok": False, "error": "This claim link is not valid"}
    item_name = pair["item_name"]

    existing = frappe.db.get_value("Customer Account", {"mobile": phone}, "name")
    if existing:
        return {"ok": True, "exists": True, "item_name": item_name,
                "name": frappe.db.get_value("Customer Account", existing, "customer_name") or ""}

    if not pin or len(pin) != 4 or not pin.isdigit():
        return {"ok": False, "error": "PIN must be 4 digits"}

    if not frappe.db.has_column("Customer Account", "address"):
        setup_customer_profile_fields()
    account = frappe.get_doc({
        "doctype": "Customer Account",
        "mobile": phone,
        "customer_name": name,
        "pin": pin,
        "address": address,
        "area": area,
        "last_login": now_datetime(),
    })
    account.flags.ignore_permissions = True
    account.insert(ignore_permissions=True)
    frappe.db.commit()
    token = _get_or_create_customer_token(phone)

    # WhatsApp confirmation — details saved, offer locked (best-effort)
    try:
        what = item_name or "meal"
        _send_wa_reply(_wa_number(phone),
            f"✅ Details saved{(', ' + name) if name else ''}!\n\n"
            f"🎁 Your FREE {what} is locked in — it's added to your FIRST order "
            f"automatically.\n⏰ Free-meal orders are for within {PROMO_WINDOW_HOURS} hours."
            f"\n📍 Pickup only — collect at our {PROMO_PICKUP_OUTLET} outlet (no delivery).\n\n"
            f"👉 Order now: https://luuvgrand.com/cus")
    except Exception as e:
        frappe.log_error("QR Promo claim WA", str(e))

    return {"ok": True, "token": token, "name": name, "item_name": item_name}


# ─── Customer profile (name / address / area — one account per number) ─────

@frappe.whitelist()
def setup_customer_profile_fields():
    """Add address + area fields to Customer Account (idempotent)."""
    dt = frappe.get_doc("DocType", "Customer Account")
    have = {f.fieldname for f in dt.fields}
    added = []
    for fn, label, ftype in [("address", "Address", "Small Text"), ("area", "Area", "Data")]:
        if fn not in have:
            dt.append("fields", {"fieldname": fn, "label": label, "fieldtype": ftype})
            added.append(fn)
    if added:
        dt.flags.ignore_permissions = True
        dt.save(ignore_permissions=True)
        frappe.db.commit()
    return {"added": added}


def _mobile_from_token(token):
    if not token:
        return None
    return frappe.db.get_value("Customer Token", {"token": token}, "mobile")


@frappe.whitelist(allow_guest=True)
def customer_get_profile():
    """Profile for the signed-in /app customer — drives the complete-your-profile
    prompt on app load (name/address/area; no OTP re-asks, token is the auth)."""
    token = (frappe.local.form_dict.get("token") or "").strip()
    mobile = _mobile_from_token(token)
    if not mobile:
        return {"success": False, "error": "Login required"}
    acc = frappe.db.get_value("Customer Account", {"mobile": mobile},
                              ["name", "customer_name"], as_dict=True)
    if not acc:
        return {"success": False, "error": "Account not found"}
    address = area = ""
    if frappe.db.has_column("Customer Account", "address"):
        address = frappe.db.get_value("Customer Account", acc.name, "address") or ""
        area = frappe.db.get_value("Customer Account", acc.name, "area") or ""
    return {"success": True, "name": acc.customer_name or "", "mobile": mobile,
            "address": address, "area": area,
            "profile_complete": bool((acc.customer_name or "") and address and area)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_update_profile():
    """Save name/address/area for the token's account (no OTP — already signed in)."""
    data = frappe.local.form_dict
    mobile = _mobile_from_token((data.get("token") or "").strip())
    if not mobile:
        return {"success": False, "error": "Login required"}
    acc_name = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")
    if not acc_name:
        return {"success": False, "error": "Account not found"}
    if not frappe.db.has_column("Customer Account", "address"):
        setup_customer_profile_fields()
    updates = {}
    for field, src in (("customer_name", "name"), ("address", "address"), ("area", "area")):
        v = (data.get(src) or "").strip()
        if v:
            updates[field] = v
    if updates:
        frappe.db.set_value("Customer Account", acc_name, updates)
        frappe.db.commit()
    return {"success": True}


# ─── WhatsApp number confirmation (OTP) for /app registration ─────

@frappe.whitelist(allow_guest=True, methods=["POST"])
def wa_send_otp():
    """WhatsApp a 4-digit code so the customer confirms the number is really theirs
    (guards the free-meal promo against made-up numbers). 30s cooldown, 5 min TTL."""
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    if not mobile or len(mobile) < 10:
        return {"sent": False, "error": "Valid mobile required"}
    # Numbers that claimed a promo VIA WhatsApp are already proven — skip the OTP.
    if frappe.db.exists("DocType", "QR Promo Code") and frappe.db.exists(
            "QR Promo Code", {"mobile": mobile, "status": ["in", ["Registered", "Redeemed"]]}):
        return {"sent": False, "verified": True}
    cache = frappe.cache()
    if cache.get_value(f"wa_otp_cool:{mobile}"):
        return {"sent": True, "cooldown": True}  # one was sent moments ago — reuse it
    code = "".join(secrets.choice("0123456789") for _ in range(4))
    ok = _send_wa_reply(_wa_number(mobile),
        f"*{code}* is your Luuv Fryxo verification code.\n\n"
        f"Enter it in the app to confirm this WhatsApp number. Valid for 5 minutes.")
    if not ok:
        return {"sent": False}
    cache.set_value(f"wa_otp:{mobile}", code, expires_in_sec=300)
    cache.set_value(f"wa_otp_cool:{mobile}", 1, expires_in_sec=30)
    return {"sent": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def wa_verify_otp():
    """Check the WhatsApp code; marks the mobile verified for 15 minutes."""
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    code = (frappe.local.form_dict.get("code") or "").strip()
    if not mobile or not code:
        return {"ok": False, "error": "Code required"}
    cache = frappe.cache()
    tries_key = f"wa_otp_tries:{mobile}"
    tries = int(cache.get_value(tries_key) or 0)
    if tries >= 5:
        return {"ok": False, "error": "Too many attempts — request a new code"}
    real = cache.get_value(f"wa_otp:{mobile}")
    if real is None:
        return {"ok": False, "error": "Code expired — tap Resend"}
    real = real.decode() if isinstance(real, bytes) else str(real)
    if code != real:
        cache.set_value(tries_key, tries + 1, expires_in_sec=300)
        return {"ok": False, "error": "Wrong code — check WhatsApp"}
    cache.delete_value(f"wa_otp:{mobile}")
    cache.delete_value(tries_key)
    cache.set_value(f"wa_verified:{mobile}", 1, expires_in_sec=900)
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
def promo_status():
    """Pending free-meal info for a mobile — powers the banner/popup in /cus.
    Includes the within-N-hours window so the client caps the time picker."""
    from frappe.utils import add_to_date
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    promo = _pending_promo_for(mobile)
    if not promo or not promo.get("free_item"):
        return {"pending": bool(promo), "item_name": ""}
    item_name = frappe.db.get_value("Item", promo["free_item"], "item_name") or promo["free_item"]
    max_time = add_to_date(now_datetime(), hours=PROMO_WINDOW_HOURS).strftime("%Y-%m-%d %H:%M:%S")
    return {"pending": True, "item_name": item_name,
            "window_hours": PROMO_WINDOW_HOURS, "max_time": max_time,
            "pickup_outlet": PROMO_PICKUP_OUTLET}


# ─── Manager menu settings (/manager Settings tab: add/edit menu items) ──────
# Price writes go to Item Price in the POS selling price list because that is
# the authority in _resolve_item_rate — pos2/app/kiosk all resolve through it,
# so an edit here shows up on the POS on its next page load.

MENU_MANAGER_ROLES = {"System Manager", "Administrator", "Restaurant Manager"}


def _require_menu_manager():
    if not (set(frappe.get_roles(frappe.session.user)) & MENU_MANAGER_ROLES):
        frappe.throw(_("Only a manager can change the menu"), frappe.PermissionError)


def _active_menu_doc():
    restaurant_name = frappe.get_value("Restaurant", {}, "name")
    active = frappe.get_value("Restaurant", restaurant_name, "active_menu") if restaurant_name else None
    if not active:
        frappe.throw(_("No active menu is configured on the Restaurant"))
    return frappe.get_doc("Restaurant Menu", active)


def _winning_pos_price_row(item_code, price_list):
    """Name of the Item Price row _resolve_item_rate would pick from the POS
    price list today (same filters + ordering), or None."""
    if not price_list:
        return None
    rows = frappe.db.sql(
        """
        SELECT name FROM `tabItem Price`
        WHERE item_code = %(item_code)s AND selling = 1 AND price_list = %(price_list)s
          AND (customer IS NULL OR customer = '')
          AND (valid_from IS NULL OR valid_from <= %(today)s)
          AND (valid_upto IS NULL OR valid_upto >= %(today)s)
        ORDER BY COALESCE(valid_from, '1900-01-01') DESC, modified DESC
        LIMIT 1
        """,
        {"item_code": item_code, "price_list": price_list, "today": frappe.utils.today()})
    return rows[0][0] if rows else None


def _set_pos_item_price(item_code, rate, price_list):
    """Upsert the POS-visible selling price: update the currently-winning Item
    Price row in the POS price list (so promo/validity rows aren't shadowed by
    a new one that would lose the newest-window-wins ordering), else insert."""
    row = _winning_pos_price_row(item_code, price_list)
    if row:
        frappe.db.set_value("Item Price", row, "price_list_rate", rate)
    elif price_list:
        frappe.get_doc({
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "selling": 1,
            "price_list_rate": rate,
        }).insert(ignore_permissions=True)
    else:
        # No POS selling price list configured — standard_rate is the last
        # fallback _resolve_item_rate reads.
        frappe.db.set_value("Item", item_code, "standard_rate", rate)


@frappe.whitelist()
def get_menu_settings():
    """Active-menu item list for the Manager Settings tab (resolved POS rates)."""
    _require_menu_manager()
    menu = _active_menu_doc()
    price_list = _get_pos_price_list()
    has_gd = frappe.db.has_column("Item", "guide_commission_percent")
    items = []
    for row in menu.items:
        if not row.item:
            continue
        fields = ["item_name", "item_group"] + (["guide_commission_percent"] if has_gd else [])
        d = frappe.db.get_value("Item", row.item, fields, as_dict=True) or {}
        items.append({
            "code": row.item,
            "name": d.get("item_name") or row.item,
            "group": d.get("item_group") or "General",
            "rate": _resolve_item_rate(row.item, row.rate, price_list),
            "gd_pct": float(d.get("guide_commission_percent") or 0),
        })
    items.sort(key=lambda x: (x["group"], x["name"]))
    groups = sorted({i["group"] for i in items})
    all_groups = frappe.get_all("Item Group", filters={"is_group": 0}, order_by="name", pluck="name")
    return {"menu": menu.name, "price_list": price_list or "",
            "items": items, "groups": groups, "all_groups": all_groups}


@frappe.whitelist(methods=["POST"])
def update_menu_item(item_code=None, item_name=None, rate=None, guide_pct=None):
    """Rename, reprice and/or set the guide-commission override for a menu item."""
    _require_menu_manager()
    item_code = (item_code or "").strip()
    if not item_code or not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item not found"))
    menu = _active_menu_doc()
    menu_row = next((r for r in menu.items if r.item == item_code), None)
    if not menu_row:
        frappe.throw(_("{0} is not on the active menu").format(item_code))

    item_name = (item_name or "").strip()
    if item_name:
        frappe.db.set_value("Item", item_code, "item_name", item_name)

    price_list = _get_pos_price_list()
    if rate not in (None, ""):
        rate = float(rate)
        if rate <= 0:
            frappe.throw(_("Price must be above zero"))
        _set_pos_item_price(item_code, rate, price_list)
        # keep the menu row's fallback rate in step (no doc save → no side effects)
        frappe.db.set_value("Restaurant Menu Item", menu_row.name, "rate", rate)

    # 0 / blank clears the override → the guide's own percent applies again
    if guide_pct is not None and frappe.db.has_column("Item", "guide_commission_percent"):
        gp = float(guide_pct or 0)
        if gp < 0 or gp > 50:
            frappe.throw(_("Guide commission must be between 0 and 50"))
        frappe.db.set_value("Item", item_code, "guide_commission_percent", gp)

    return {"ok": True, "code": item_code,
            "name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
            "rate": _resolve_item_rate(item_code, 0, price_list)}


@frappe.whitelist(methods=["POST"])
def add_menu_item(item_name=None, rate=None, group=None, guide_pct=None):
    """Create an Item (non-stock), price it, and append it to the active menu."""
    _require_menu_manager()
    item_name = (item_name or "").strip()
    if not item_name:
        frappe.throw(_("Enter an item name"))
    rate = float(rate or 0)
    if rate <= 0:
        frappe.throw(_("Price must be above zero"))
    group = (group or "").strip()
    if not group or not frappe.db.exists("Item Group", group):
        frappe.throw(_("Pick a valid category"))

    menu = _active_menu_doc()
    item_code = item_name  # menu items here use the name as the code (matches existing items)
    if frappe.db.exists("Item", item_code):
        if any(r.item == item_code for r in menu.items):
            frappe.throw(_("{0} is already on the menu").format(item_code))
    else:
        frappe.get_doc({
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_name,
            "item_group": group,
            "stock_uom": "Nos",
            "is_stock_item": 0,
        }).insert(ignore_permissions=True)

    price_list = _get_pos_price_list()
    _set_pos_item_price(item_code, rate, price_list)

    if guide_pct not in (None, "") and frappe.db.has_column("Item", "guide_commission_percent"):
        gp = float(guide_pct or 0)
        if gp < 0 or gp > 50:
            frappe.throw(_("Guide commission must be between 0 and 50"))
        frappe.db.set_value("Item", item_code, "guide_commission_percent", gp)

    menu.append("items", {"item": item_code, "rate": rate})
    menu.save(ignore_permissions=True)

    return {"ok": True, "code": item_code, "name": item_name,
            "rate": _resolve_item_rate(item_code, rate, price_list), "group": group}


# ─── /app routing: Frappe Desk vs customer PWA ───────────────────────────────
# The customer PWA used to live at /app, which shadowed Frappe Desk (Desk is
# served by frappe/www/app.py, and a later-installed app's www/app.* wins).
# The PWA now lives at /cus; this before_request hook keeps every old
# /app entry point working: WhatsApp links (/app?phone=), referral links
# (/app?ref=), promo landings (/app?promo=) and installed PWAs whose
# start_url is still /app. Desk users get Desk; everyone else gets /cus.

def route_app_requests():
    """before_request hook (hooks.py). Bare /app is pure Frappe (login + Desk);
    only customer deep links — ?promo= (promo landings), ?ref= (referrals),
    ?phone= (WhatsApp) — are redirected to the customer PWA at /cus,
    preserving the query string. Desk subpaths (/app/<doctype>...) are never
    touched. (Installed customer PWAs from the /app era land on the login
    page; they reinstall from /cus — acceptable, the app just launched.)"""
    request = getattr(frappe.local, "request", None)
    if request is None or request.path.rstrip("/") != "/app":
        return
    deep_link = request.args.get("promo") or request.args.get("ref") or request.args.get("phone")
    if not deep_link:
        return  # bare /app → Frappe default (login screen / Desk)
    qs = request.query_string.decode() if request.query_string else ""
    location = "/cus" + (("?" + qs) if qs else "")
    # Redirect from a before_request hook: frappe.Redirect here isn't special-cased by
    # handle_exception (it renders a 301 "message" page with NO Location). app.py instead
    # does `except HTTPException: return e` — so raise a werkzeug redirect (an HTTPException),
    # which is returned directly with a proper Location header.
    from werkzeug.routing import RequestRedirect
    exc = RequestRedirect(location)
    exc.code = 302
    raise exc


# ─── Tour Guide Cashback ─────────────────────────────────────────────────────
# Tour guides (and the bus crew — driver/helper) who bring customers earn an
# automatic cashback: each partner's OWN percent of the bill, credited when the
# bill is paid (place_order / process_payment / pay_split). Balances are paid
# out later by a manager. Payouts are record-only — never a POS Cash Movement,
# for the same reason create_expense() stays out of the drawer: pos2's
# client-side close reconciliation and ERPNext's expected_amount both ignore
# drawer pay-outs, so writing one would make the close look short.
# Guides are external people — no Frappe Users. The /guide PWA authenticates
# with a Guide Token (Customer Token pattern) against a salted-hash password
# that is generated at quick-add and sent once over a wa.me invite.

GUIDE_DEFAULT_PERCENT = 5.0
GUIDE_MAX_PER_BILL = 3
GUIDE_APP_URL = "https://luuvgrand.com/guide"
# Invite codes gate NEW self-registrations over WhatsApp: without one, a stray
# "guide" in any customer message would create a Tour Guide record. Same
# unambiguous alphabet as the QR promo codes (no 0/O, 1/I/L).
GUIDE_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
GUIDE_CODE_RE = r"\bGD[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}\b"
# False → cashback base = food subtotal minus discount (the service charge is a
# tax row, not customer spend). True → base = invoice grand total.
GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE = False


@frappe.whitelist()
def setup_tour_guides():
    """One-time setup: Tour Guide + Guide Token + Guide Cashback Txn DocTypes.
    Run: bench --site <site> execute luuvcrm.api.setup_tour_guides"""
    created = []

    if not frappe.db.exists("DocType", "Tour Guide"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Tour Guide", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "guide_name", "label": "Guide Name", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "whatsapp", "label": "WhatsApp", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "role", "label": "Role", "fieldtype": "Select", "options": "Guide\nDriver\nHelper", "default": "Guide", "in_list_view": 1},
                {"fieldname": "cashback_percent", "label": "Cashback %", "fieldtype": "Percent", "default": GUIDE_DEFAULT_PERCENT},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Invited\nActive", "default": "Invited", "in_list_view": 1},
                {"fieldname": "password_hash", "label": "Password Hash", "fieldtype": "Data"},
                {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_paid", "label": "Total Paid", "fieldtype": "Currency", "default": 0},
                {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
                {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1, "write": 1, "create": 1},
            ],
        }).insert()
        created.append("Tour Guide")

    if not frappe.db.exists("DocType", "Guide Token"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Token", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
                {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1},
                {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert()
        created.append("Guide Token")

    if not frappe.db.exists("DocType", "Guide Cashback Txn"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Cashback Txn", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1, "in_list_view": 1},
                {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nPayout", "reqd": 1, "in_list_view": 1},
                {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
                {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
                {"fieldname": "reference_invoice", "label": "Reference Invoice", "fieldtype": "Data"},
                {"fieldname": "bill_total", "label": "Bill Total (base)", "fieldtype": "Currency"},
                {"fieldname": "percent_used", "label": "Percent Used", "fieldtype": "Percent"},
                {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
                {"fieldname": "paid_by", "label": "Paid By", "fieldtype": "Link", "options": "User"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1},
            ],
        }).insert()
        created.append("Guide Cashback Txn")

    if not frappe.db.exists("DocType", "Guide Invite Code"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Invite Code", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "code", "label": "Code", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "New\nUsed", "default": "New", "in_list_view": 1},
                {"fieldname": "used_by", "label": "Used By", "fieldtype": "Link", "options": "Tour Guide", "in_list_view": 1},
                {"fieldname": "used_on", "label": "Used On", "fieldtype": "Datetime"},
                {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1},
            ],
        }).insert()
        created.append("Guide Invite Code")

    # "Redeem" (guide pays their own meal from balance) was added after the
    # doctype first shipped — register the option on existing installs too.
    if frappe.db.exists("DocType", "Guide Cashback Txn"):
        _ensure_select_option("Guide Cashback Txn", "type", "Redeem")

    # Global default % — manager-editable (Loyalty Settings pattern); replaces
    # the hardcoded GUIDE_DEFAULT_PERCENT for new guides and as the fallback.
    if not frappe.db.exists("DocType", "Guide Settings"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Settings", "module": "Zeloura",
            "custom": 1, "issingle": 1,
            "fields": [
                {"fieldname": "default_percent", "label": "Default Cashback %",
                 "fieldtype": "Percent", "default": GUIDE_DEFAULT_PERCENT},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert()
        created.append("Guide Settings")
    if frappe.db.exists("DocType", "Guide Settings") \
            and not frappe.db.exists("Guide Settings", "Guide Settings"):
        s = frappe.get_doc({"doctype": "Guide Settings"})
        s.flags.ignore_permissions = True
        s.insert()

    # Item-wise commission override: an Item's guide_commission_percent beats
    # the guide's own percent for that line (most-specific wins).
    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "guide_commission_percent"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field", "dt": "Item",
            "fieldname": "guide_commission_percent", "label": "Guide Commission %",
            "fieldtype": "Percent", "insert_after": "standard_rate",
        })
        cf.flags.ignore_permissions = True
        cf.insert()
        created.append("Item.guide_commission_percent")

    return {"status": "created" if created else "already_exists", "created": created}


def _guide_default_percent():
    """Manager-set global cashback % (Guide Settings single), else the code default."""
    try:
        if frappe.db.exists("DocType", "Guide Settings"):
            v = frappe.db.get_single_value("Guide Settings", "default_percent")
            if v is not None and float(v) > 0:
                return float(v)
    except Exception:
        pass
    return GUIDE_DEFAULT_PERCENT


def _normalize_wa_number(mobile, dial="94"):
    """Digits-only international form (94XXXXXXXXX): strip leading zeros and
    prepend the dial code if missing — same normalization the waiter page does
    client-side before building wa.me links."""
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    digits = digits.lstrip("0")
    if digits and not digits.startswith(dial):
        digits = dial + digits
    if len(digits) < 10:
        frappe.throw(_("Enter a valid WhatsApp number"))
    return digits


def _hash_guide_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + (pw or '')).encode()).hexdigest()}"


def _verify_guide_pw(pw, stored):
    if not stored or "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return secrets.compare_digest(_hash_guide_pw(pw, salt), stored)


def _gen_guide_password():
    """8 chars from an unambiguous alphabet (no 0/O, 1/I/L) — typed off a
    WhatsApp message on a phone."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _gen_guide_code():
    for _ in range(50):
        code = "GD" + "".join(secrets.choice(GUIDE_CODE_ALPHABET) for _ in range(6))
        if not frappe.db.exists("Guide Invite Code", {"code": code}):
            return code
    return "GD" + secrets.token_hex(3).upper()


@frappe.whitelist(methods=["POST"])
def create_guide_codes(count=5):
    """Mint partner invite codes (manager). Each code registers exactly one new
    guide over WhatsApp; staff hand them out in person or share the wa.me link."""
    _require_manager()
    count = max(1, min(int(count or 5), 50))
    codes = []
    for _ in range(count):
        code = _gen_guide_code()
        frappe.get_doc({
            "doctype": "Guide Invite Code", "code": code, "status": "New",
            "created_by_user": frappe.session.user,
        }).insert(ignore_permissions=True)
        codes.append(code)
    frappe.db.commit()
    return {"ok": True, "codes": codes}


@frappe.whitelist()
def get_guide_codes():
    _require_manager()
    fresh = frappe.get_all("Guide Invite Code", filters={"status": "New"},
        fields=["code", "creation"], order_by="creation desc", limit=50)
    used = frappe.get_all("Guide Invite Code", filters={"status": "Used"},
        fields=["code", "used_by", "used_on"], order_by="used_on desc", limit=20)
    for u in used:
        u["guide_name"] = (frappe.db.get_value("Tour Guide", u["used_by"],
                           "guide_name") or "") if u.get("used_by") else ""
        u["used_on"] = str(u.get("used_on") or "")[:16]
    for f in fresh:
        f["creation"] = str(f["creation"])[:10]
    return {"fresh": fresh, "used": used}


def _guide_invite_text(token, percent, guide_name=""):
    """Magic-link invite: tapping the link logs the guide straight in (the token
    IS the session) — no password to type. WhatsApp possession is the auth root."""
    hello = f"Hi {guide_name}!" if guide_name else "Hi!"
    return (f"{hello} Welcome to Luuv Fryxo's partner program. "
            f"You earn {percent:g}% cashback on every bill you bring us. "
            f"Tap your personal link to open your cashback account:\n"
            f"{GUIDE_APP_URL}?t={token}")


# ── Cashier-facing (POS session) ──

@frappe.whitelist()
def guide_search(mobile=None):
    """Find a Tour Guide by WhatsApp number for the POS payment dialog."""
    _require_pos_role()
    number = _normalize_wa_number(mobile)
    name = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if not name:
        return {"found": False, "whatsapp": number}
    g = frappe.db.get_value("Tour Guide", name,
        ["guide_name", "role", "cashback_percent", "status", "cashback_balance"], as_dict=True)
    return {"found": True, "name": name, "guide_name": g.guide_name or "",
            "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
            "status": g.status, "whatsapp": number,
            "cashback_balance": float(g.cashback_balance or 0)}


@frappe.whitelist(methods=["POST"])
def guide_quick_add(mobile=None, name="", role="Guide", percent=None):
    """Create an Invited guide from the POS. Returns the generated password and
    ready-made invite text EXACTLY ONCE so the client can open the wa.me link —
    only the salted hash is stored."""
    _require_pos_role()
    number = _normalize_wa_number(mobile)
    existing = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if existing:
        g = frappe.db.get_value("Tour Guide", existing,
            ["guide_name", "role", "cashback_percent", "status"], as_dict=True)
        return {"found": True, "name": existing, "guide_name": g.guide_name or "",
                "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
                "status": g.status, "whatsapp": number}
    if role not in ("Guide", "Driver", "Helper"):
        role = "Guide"
    pct = float(percent) if percent not in (None, "") else _guide_default_percent()
    if pct < 0 or pct > 50:
        frappe.throw(_("Cashback percent must be between 0 and 50"))
    guide = frappe.get_doc({
        "doctype": "Tour Guide",
        "guide_name": (name or "").strip(),
        "whatsapp": number,
        "role": role,
        "cashback_percent": pct,
        "status": "Invited",
        # random unused hash — login is via the magic-link token, not a password
        "password_hash": _hash_guide_pw(_gen_guide_password()),
        "created_by_user": frappe.session.user,
    })
    guide.insert(ignore_permissions=True)
    token = _get_or_create_guide_token(guide.name)
    frappe.db.commit()
    return {"found": True, "created": True, "name": guide.name,
            "guide_name": guide.guide_name, "role": role, "cashback_percent": pct,
            "status": "Invited", "whatsapp": number,
            "invite_text": _guide_invite_text(token, pct, guide.guide_name)}


# ── Settlement hook ──

def _notify_guide_earning(guide, amount, order_name):
    """Best-effort WhatsApp to the guide the moment a bill credits them: this
    bill's cashback, today's running total, and the full balance. Rides the
    Cloud API text send — delivery only works inside Meta's 24-hour service
    window (opened by the guide's OWN messages), so failures are expected and
    swallowed; the payment must never notice."""
    try:
        today_total = frappe.db.sql("""
            select coalesce(sum(amount),0) from `tabGuide Cashback Txn`
            where guide=%s and type='Earn' and creation >= %s
        """, (guide.name, frappe.utils.today()))[0][0]
        token = _get_or_create_guide_token(guide.name)
        hello = f"Hi {guide.guide_name}!" if (guide.guide_name or "").strip() else "Hi!"
        _send_wa_reply(guide.whatsapp,
            f"🧭 {hello} A bill you brought us was just paid.\n\n"
            f"💰 This bill: Rs {amount:,.2f}\n"
            f"📅 Today so far: Rs {float(today_total):,.2f}\n"
            f"🏦 Your balance: Rs {float(guide.cashback_balance or 0):,.2f}\n\n"
            f"Details: {GUIDE_APP_URL}?t={token}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Guide WA notify")


def _parse_guide_redeem(redeem_param, bill_total):
    """Validate a {guide, amount} balance-redemption request (guide eats and pays
    from their cashback). THROWS on any problem — a redemption the guide believes
    covered the bill must never silently no-op. Returns (guide_name, amount)."""
    if not redeem_param:
        return None, 0.0
    req = frappe.parse_json(redeem_param) if isinstance(redeem_param, str) else redeem_param
    gname = (req or {}).get("guide")
    amt = round(float((req or {}).get("amount") or 0), 2)
    if not gname or amt <= 0:
        return None, 0.0
    if not frappe.db.exists("Tour Guide", gname):
        frappe.throw(_("Guide not found for balance redemption"))
    balance = float(frappe.db.get_value("Tour Guide", gname, "cashback_balance") or 0)
    if amt > balance + 0.005:
        frappe.throw(_("Guide balance is only Rs {0:,.2f}").format(balance))
    if amt > float(bill_total or 0) + 0.005:
        frappe.throw(_("Redemption can't exceed the bill total"))
    return gname, amt


def _finalize_guide_redeem(gname, amount, order_name, invoice_name):
    """Deduct the redeemed amount from the guide's balance + log a Redeem txn.
    Called after invoice submit; the invoice already carries the amount as a
    Net Total discount, so the payment rows only cover what the customer paid."""
    if not gname or amount <= 0:
        return
    _ensure_select_option("Guide Cashback Txn", "type", "Redeem")
    guide = frappe.get_doc("Tour Guide", gname)
    guide.cashback_balance = (guide.cashback_balance or 0) - amount
    guide.save(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Guide Cashback Txn",
        "guide": gname,
        "type": "Redeem",
        "amount": amount,
        "reference_order": order_name or "",
        "reference_invoice": invoice_name or "",
        "note": "Bill paid from cashback balance",
    }).insert(ignore_permissions=True)
    try:
        token = _get_or_create_guide_token(guide.name)
        _send_wa_reply(guide.whatsapp,
            f"🍔 Rs {amount:,.2f} was paid from your Luuv Fryxo cashback balance"
            f"{(' for bill ' + order_name) if order_name else ''}.\n\n"
            f"🏦 Remaining balance: Rs {float(guide.cashback_balance or 0):,.2f}\n\n"
            f"Details: {GUIDE_APP_URL}?t={token}")
    except Exception:
        pass
    frappe.db.commit()


def _credit_guide_cashback(guides_param, base_amount, order_name, invoice_name, items=None):
    """Credit each attached partner their cashback and log an Earn txn.
    Idempotent per (guide, invoice). Never raises — a cashback problem must not
    fail the payment it rides on.

    Rate hierarchy (most specific wins): an Item's guide_commission_percent
    custom field overrides the guide's own percent for that line; the guide's
    percent (defaulted from Guide Settings at creation) covers the rest.
    base_amount = subtotal − discount, so the per-item math is scaled by
    base/subtotal and bill discounts shrink commissions proportionally."""
    try:
        if not guides_param or not frappe.db.exists("DocType", "Guide Cashback Txn"):
            return
        guides = frappe.parse_json(guides_param) if isinstance(guides_param, str) else guides_param
        base = float(base_amount or 0)
        if not guides or base <= 0:
            return
        items = items or []
        subtotal = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0) for i in items)
        item_pct = {}
        if items and frappe.db.has_column("Item", "guide_commission_percent"):
            for i in items:
                code = i.get("item")
                if code and code not in item_pct:
                    item_pct[code] = float(frappe.db.get_value(
                        "Item", code, "guide_commission_percent") or 0)
        for gname in list(dict.fromkeys(guides))[:GUIDE_MAX_PER_BILL]:
            if not frappe.db.exists("Tour Guide", gname):
                continue
            if invoice_name and frappe.db.exists("Guide Cashback Txn",
                    {"guide": gname, "type": "Earn", "reference_invoice": invoice_name}):
                continue  # client retry — this bill already credited this guide
            if invoice_name and frappe.db.exists("Guide Cashback Txn",
                    {"guide": gname, "type": "Redeem", "reference_invoice": invoice_name}):
                continue  # guide paid this bill FROM their balance — no earning on it
            guide = frappe.get_doc("Tour Guide", gname)
            pct = float(guide.cashback_percent or 0)
            if items and subtotal > 0:
                raw = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0)
                          * ((item_pct.get(i.get("item")) or pct)) / 100.0
                          for i in items)
                amount = round(raw * (base / subtotal), 2)
            else:
                amount = round(base * pct / 100, 2)
            if amount <= 0:
                continue
            eff_pct = round(amount * 100.0 / base, 2) if base > 0 else pct
            guide.cashback_balance = (guide.cashback_balance or 0) + amount
            guide.total_earned = (guide.total_earned or 0) + amount
            guide.save(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Guide Cashback Txn",
                "guide": gname,
                "type": "Earn",
                "amount": amount,
                "reference_order": order_name or "",
                "reference_invoice": invoice_name or "",
                "bill_total": base,
                "percent_used": eff_pct,
            }).insert(ignore_permissions=True)
            _notify_guide_earning(guide, amount, order_name)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Guide Cashback")


# ── Guide app (/guide PWA — token auth, no Frappe Users) ──

def _get_or_create_guide_token(guide_name):
    # 90 days, not the customer scheme's 7: the token doubles as the magic-link
    # login (guides may only visit monthly); an expired one just means messaging
    # GUIDE on WhatsApp again for a fresh link.
    frappe.db.sql("DELETE FROM `tabGuide Token` WHERE created < NOW() - INTERVAL 90 DAY")
    frappe.db.commit()
    existing = frappe.db.get_value("Guide Token", {"guide": guide_name}, "token")
    if existing:
        return existing
    token = secrets.token_hex(16)
    frappe.get_doc({
        "doctype": "Guide Token", "token": token,
        "guide": guide_name, "created": now_datetime(),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return token


def _guide_from_token(token):
    if not token:
        return None
    return frappe.db.get_value("Guide Token", {"token": str(token).strip()}, "guide")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def guide_login(mobile=None, password=None):
    number = _normalize_wa_number(mobile)
    name = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if not name:
        return {"success": False, "error": "No guide account for this number"}
    stored = frappe.db.get_value("Tour Guide", name, "password_hash")
    if not _verify_guide_pw((password or "").strip(), stored):
        return {"success": False, "error": "Wrong password"}
    guide = frappe.get_doc("Tour Guide", name)
    if guide.status != "Active":
        guide.db_set("status", "Active")
    return {"success": True, "token": _get_or_create_guide_token(name),
            "guide_name": guide.guide_name or "", "role": guide.role or "Guide",
            "cashback_percent": float(guide.cashback_percent or 0),
            "needs_profile": not (guide.guide_name or "").strip()}


@frappe.whitelist(allow_guest=True)
def guide_dashboard(token=None):
    name = _guide_from_token(token)
    if not name:
        return {"success": False, "error": "Login required"}
    g = frappe.get_doc("Tour Guide", name)
    txns = frappe.get_all("Guide Cashback Txn",
        filters={"guide": name},
        fields=["type", "amount", "reference_order", "bill_total", "percent_used",
                "note", "creation"],
        order_by="creation desc", limit=50)
    for t in txns:
        t["creation"] = str(t["creation"])[:16]
    return {"success": True,
            "guide_name": g.guide_name or "", "whatsapp": g.whatsapp,
            "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
            "cashback_balance": float(g.cashback_balance or 0),
            "total_earned": float(g.total_earned or 0),
            "total_paid": float(g.total_paid or 0),
            "earnings": [t for t in txns if t["type"] == "Earn"],
            "payouts": [t for t in txns if t["type"] != "Earn"]}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def guide_update_profile(token=None, guide_name=None, notes=None):
    name = _guide_from_token(token)
    if not name:
        return {"success": False, "error": "Login required"}
    guide = frappe.get_doc("Tour Guide", name)
    if guide_name is not None and str(guide_name).strip():
        guide.guide_name = str(guide_name).strip()
    if notes is not None:
        guide.notes = str(notes).strip()
    guide.save(ignore_permissions=True)
    frappe.db.commit()
    return {"success": True, "guide_name": guide.guide_name or ""}


# ── Manager (list, payout, edit) ──

def _require_manager():
    if not (set(frappe.get_roles(frappe.session.user)) & MENU_MANAGER_ROLES):
        frappe.throw(_("Only a manager can do this"), frappe.PermissionError)


@frappe.whitelist()
def list_guides():
    _require_manager()
    guides = frappe.get_all("Tour Guide",
        fields=["name", "guide_name", "whatsapp", "role", "cashback_percent",
                "status", "cashback_balance", "total_earned", "total_paid"],
        order_by="cashback_balance desc, guide_name asc")
    for g in guides:
        for f in ("cashback_percent", "cashback_balance", "total_earned", "total_paid"):
            g[f] = float(g[f] or 0)
    return {"guides": guides, "default_percent": _guide_default_percent()}


@frappe.whitelist(methods=["POST"])
def update_guide_settings(percent=None):
    """Set the global default cashback % (used for every newly registered guide;
    existing guides keep their own percent — edit those per guide)."""
    _require_manager()
    pct = float(percent)
    if pct < 0 or pct > 50:
        frappe.throw(_("Cashback percent must be between 0 and 50"))
    doc = frappe.get_doc("Guide Settings")
    doc.default_percent = pct
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "default_percent": pct}


@frappe.whitelist()
def guide_detail(guide=None):
    _require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    txns = frappe.get_all("Guide Cashback Txn",
        filters={"guide": g.name},
        fields=["type", "amount", "reference_order", "bill_total", "percent_used",
                "note", "paid_by", "creation"],
        order_by="creation desc", limit=100)
    for t in txns:
        t["creation"] = str(t["creation"])[:16]
    return {"name": g.name, "guide_name": g.guide_name or "", "whatsapp": g.whatsapp,
            "role": g.role or "Guide", "status": g.status,
            "cashback_percent": float(g.cashback_percent or 0),
            "cashback_balance": float(g.cashback_balance or 0),
            "total_earned": float(g.total_earned or 0),
            "total_paid": float(g.total_paid or 0),
            "notes": g.notes or "", "txns": txns}


@frappe.whitelist(methods=["POST"])
def record_guide_payout(guide=None, amount=None, note=""):
    """Pay a guide part/all of their balance. RECORD-ONLY: this deliberately
    does NOT write a POS Cash Movement — pos2's client-side close recon and
    ERPNext's expected_amount both ignore drawer pay-outs (same rationale as
    create_expense), so the drawer is left untouched and the payout lives only
    in the guide's ledger."""
    _require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    amt = float(amount or 0)
    balance = float(g.cashback_balance or 0)
    if amt <= 0:
        frappe.throw(_("Enter a payout amount above zero"))
    if amt > balance + 0.005:
        frappe.throw(_("Payout exceeds balance (Rs {0:,.2f})").format(balance))
    g.cashback_balance = balance - amt
    g.total_paid = (g.total_paid or 0) + amt
    g.save(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Guide Cashback Txn",
        "guide": g.name,
        "type": "Payout",
        "amount": amt,
        "note": (note or "").strip(),
        "paid_by": frappe.session.user,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "cashback_balance": float(g.cashback_balance or 0),
            "total_paid": float(g.total_paid or 0)}


@frappe.whitelist(methods=["POST"])
def update_guide(guide=None, guide_name=None, role=None, percent=None, status=None):
    _require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    if guide_name is not None:
        g.guide_name = str(guide_name).strip()
    if role in ("Guide", "Driver", "Helper"):
        g.role = role
    if percent not in (None, ""):
        pct = float(percent)
        if pct < 0 or pct > 50:
            frappe.throw(_("Cashback percent must be between 0 and 50"))
        g.cashback_percent = pct
    if status in ("Invited", "Active"):
        g.status = status
    g.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}


# ─── Thermal print formats: FOOD / BEVERAGES / TAKEAWAY sections ────────────
# The three DB Print Formats are the PRIMARY print path (render_pos_print);
# pos2's built-in builders are only the fallback. Their HTML lives here so it
# is version-controlled; setup_print_sections() pushes it into the DB docs.
# The Jinja classifier mirrors _is_beverage_item(): Item Group first, item-name
# keywords as fallback. Section headers print only when >1 section has items.

_PF_BEV_JINJA = '''{%- set BG = ["beverage","drink","juice","shake","tea","coffee","mojito","smoothie","bar"] -%}
{%- set BN = ["juice","cola","coke","sprite","soda","shake","tea","coffee","cappuccino","latte","mojito","beer","smoothie","milo","lassi","faluda","water","nestomalt"] -%}'''

KOT_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 11px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1.5mm 3mm; }
.type-banner { text-align: center; font-size: 14px; font-weight: 900; color: #000; border: 2px solid #000; border-radius: 3px; padding: 5px 4px; margin: 0 0 5px; word-break: break-word; overflow-wrap: break-word; }
.dv { border-top: 1px dashed #000; margin: 4px 0; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 3px 0; }
table.it th { font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 2px solid #000; padding: 4px 0; }
table.it th:nth-child(1) { text-align: left; width: 78%; }
table.it th:nth-child(2) { text-align: center; width: 22%; }
table.it td { padding: 4px 0; font-size: 14px; font-weight: 700; vertical-align: top; color: #000; word-break: break-word; overflow-wrap: break-word; }
table.it td:nth-child(1) { text-align: left; }
table.it td:nth-child(2) { text-align: center; font-weight: 800; }
table.it td.sect { text-align: center; font-weight: 900; font-size: 11px; letter-spacing: 1.5px; padding: 5px 0 2px; border-top: 1px dashed #000; }
.nt { font-size: 11px; font-weight: 600; font-style: italic; }
.fm { text-align: center; margin-top: 5px; font-size: 11px; color: #000; font-weight: 700; text-transform: uppercase; }
</style>
<div class="type-banner">
{%- set ot = (doc.order_type or "Dine In") -%}
{%- if ot == "Take Away" -%}Take Away
{%- elif ot == "Delivery" -%}Delivery
{%- else -%}Dine in{%- if doc.restaurant_table %} - T{{ doc.restaurant_table.split('-')[-1] }}{%- endif -%}
{%- endif -%}, {{ frappe.utils.format_datetime(doc.creation, "dd,MMM, hh.mma") }}
</div>
''' + _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[], n=0) -%}
{%- for item in doc.items -%}
  {%- set nq = (item.qty or 0) - (item.sent_qty or 0) -%}
  {%- if nq > 0 -%}
    {%- set S.n = S.n + 1 -%}
    {%- if item.takeaway -%}
      {%- set S.ta = S.ta + [(item, nq)] -%}
    {%- else -%}
      {%- set grp = (frappe.db.get_value("Item", item.item, "item_group") or "") | lower -%}
      {%- set nm = (item.item_name or item.item) | lower -%}
      {%- set B = namespace(v=false) -%}
      {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
      {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
      {%- if B.v -%}{%- set S.bev = S.bev + [(item, nq)] -%}{%- else -%}{%- set S.food = S.food + [(item, nq)] -%}{%- endif -%}
    {%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}
<table class="it"><thead><tr><th>Item</th><th>Qty</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item, nq in sec_items -%}
<tr><td>{% if item.takeaway %}(takeaway) {% endif %}{{ item.item_name or item.item }}{% if item.notes %}<div class="nt">&rarr; {{ item.notes }}</div>{% endif %}</td><td>{{ nq }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
{%- if S.n == 0 -%}
<tr><td colspan="2" style="text-align:center;padding:8px 0;font-weight:600;">No new items</td></tr>
{%- endif -%}
</tbody></table>
<div class="dv"></div>
<div class="fm">Kitchen Order Ticket</div>
'''

_PF_BILL_CSS_EXTRA = '''table.it td.sect { text-align: center; font-weight: 900; font-size: 9px; letter-spacing: 1.5px; padding: 4px 0 1px; border-top: 1px dashed #000; }
'''

_PF_ORDER_SECTION_JINJA = _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[]) -%}
{%- for item in doc.items -%}
  {%- if item.takeaway -%}
    {%- set S.ta = S.ta + [item] -%}
  {%- else -%}
    {%- set grp = (frappe.db.get_value("Item", item.item, "item_group") or "") | lower -%}
    {%- set nm = (item.item_name or item.item) | lower -%}
    {%- set B = namespace(v=false) -%}
    {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- if B.v -%}{%- set S.bev = S.bev + [item] -%}{%- else -%}{%- set S.food = S.food + [item] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}'''

GUEST_CHECK_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1mm 2mm; }
.r-header { text-align: center; margin-bottom: 3px; }
.r-header h1 { font-size: 14px; font-weight: 900; letter-spacing: 2px; text-transform: uppercase; margin: 0; color: #000; }
.r-header h2 { font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin: 1px 0 2px; color: #000; }
.r-header .sub { font-size: 8px; color: #000; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
.sec { border-top: 1px solid #000; margin: 4px 0; }
.i-row { display: flex; justify-content: space-between; font-size: 9px; padding: 1px 0; }
.i-row .lb { color: #000; font-weight: 600; }
.i-row .vl { font-weight: 700; text-align: right; color: #000; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 2px 0; }
table.it th { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 1px solid #000; padding: 3px 0; }
table.it th:nth-child(1) { text-align: left; width: 64%; }
table.it th:nth-child(2) { text-align: right; width: 36%; }
table.it td { padding: 2px 0; font-size: 10px; font-weight: 700; vertical-align: top; color: #000; }
table.it td:nth-child(1) { text-align: left; word-break: break-word; overflow-wrap: break-word; }
/* Amount must never wrap: table-layout is fixed, so a break here would split "Rs.24,500" across two lines. */
table.it td:nth-child(2) { text-align: right; font-weight: 800; white-space: nowrap; }
''' + _PF_BILL_CSS_EXTRA + '''table.tt { width: 100%; border-collapse: collapse; margin: 2px 0; }
table.tt td { padding: 1px 0; font-size: 10px; font-weight: 700; color: #000; }
table.tt td:nth-child(1) { text-align: left; width: 62%; }
table.tt td:nth-child(2) { text-align: right; width: 38%; white-space: nowrap; }
table.tt .gt td { font-size: 12px; font-weight: 900; padding: 3px 0; border-top: 1px solid #000; }
.fm { text-align: center; margin-top: 4px; font-size: 8px; color: #000; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
.note { text-align: center; font-size: 7px; font-weight: 600; margin-top: 2px; }
</style>
<div class="r-header"><h1>Luuv Fryxo</h1><h2>Guest Check</h2><div class="sub">Dambulla</div></div>
<div class="i-row"><span class="lb">Order</span><span class="vl">{{ doc.name }}</span></div>
<div class="i-row"><span class="lb">Date</span><span class="vl">{{ frappe.utils.format_datetime(doc.creation, "dd MMM, hh:mm a") }}</span></div>
{% if doc.restaurant_table %}<div class="i-row"><span class="lb">Table</span><span class="vl">{{ doc.restaurant_table }}</span></div>{% endif %}
{% if doc.customer_name %}<div class="i-row"><span class="lb">Customer</span><span class="vl">{{ doc.customer_name }}</span></div>{% endif %}
{% if doc.waiter_name %}<div class="i-row"><span class="lb">Server</span><span class="vl">{{ doc.waiter_name }}</span></div>{% endif %}
<div class="sec"></div>
{%- set ns = namespace(sub=0) -%}
{%- for i in doc.items -%}{%- set ns.sub = ns.sub + (i.qty * i.rate) -%}{%- endfor -%}
''' + _PF_ORDER_SECTION_JINJA + '''
<table class="it"><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item in sec_items -%}
<tr><td>{% if item.takeaway %}(takeaway) {% endif %}{{ item.item_name or item.item }} X{{ item.qty }}</td><td>Rs.{{ frappe.utils.fmt_money(item.qty * item.rate, 0) }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
</tbody></table>
<div class="sec"></div>
<table class="tt"><tbody>
<tr><td>Subtotal</td><td>Rs.{{ frappe.utils.fmt_money(ns.sub, 0) }}</td></tr>
{%- if doc.get("discount_amount") and doc.discount_amount > 0 -%}
<tr><td>Discount{% if doc.get("discount_note") %} ({{ doc.discount_note }}){% endif %}</td><td>&minus; Rs.{{ frappe.utils.fmt_money(doc.discount_amount, 0) }}</td></tr>
{%- endif -%}
{%- if doc.service_charge_amount and doc.service_charge_amount > 0 -%}
<tr><td>Service charge{% if doc.service_charge_rate %} ({{ doc.service_charge_rate }}%){% endif %}</td><td>Rs.{{ frappe.utils.fmt_money(doc.service_charge_amount, 0) }}</td></tr>
{%- endif -%}
<tr class="gt"><td>Total</td><td>Rs.{{ frappe.utils.fmt_money(doc.grand_total, 0) }}</td></tr>
</tbody></table>
{%- if S.ta -%}<div class="note">Take-away items carry no service charge.</div>{%- endif -%}
<div class="sec"></div>
<div class="fm">Guest check &mdash; not a final bill</div>
'''

RECEIPT_PF_HTML = '''<style>
@page { size: 80mm auto; margin: 0; }
* { box-sizing: border-box; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9px; color: #000; background: #fff; margin: 0; padding: 0; }
.print-format { width: 100%; max-width: 80mm; margin: 0; padding: 1mm 1mm 2mm; }
.r-header { text-align: center; margin-bottom: 3px; }
.r-header h1 { font-size: 14px; font-weight: 900; letter-spacing: 2px; text-transform: uppercase; margin: 0; color: #000; }
.r-header h2 { font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; margin: 1px 0 2px; color: #000; }
.r-header .sub { font-size: 8px; color: #000; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; }
.sec { border-top: 1px solid #000; margin: 4px 0; }
.i-row { display: flex; justify-content: space-between; font-size: 9px; padding: 1px 0; }
.i-row .lb { color: #000; font-weight: 600; }
.i-row .vl { font-weight: 700; text-align: right; color: #000; }
table.it { width: 100%; table-layout: fixed; border-collapse: collapse; margin: 2px 0; }
table.it th { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px; color: #000; border-bottom: 1px solid #000; padding: 3px 0; }
table.it th:nth-child(1) { text-align: left; width: 64%; }
table.it th:nth-child(2) { text-align: right; width: 36%; }
table.it td { padding: 2px 0; font-size: 10px; font-weight: 700; vertical-align: top; color: #000; }
table.it td:nth-child(1) { text-align: left; word-break: break-word; overflow-wrap: break-word; }
/* Amount must never wrap: table-layout is fixed, so a break here would split "Rs.24,500" across two lines. */
table.it td:nth-child(2) { text-align: right; font-weight: 800; white-space: nowrap; }
''' + _PF_BILL_CSS_EXTRA + '''table.tt { width: 100%; border-collapse: collapse; margin: 2px 0; }
table.tt td { padding: 1px 0; font-size: 10px; font-weight: 700; color: #000; }
table.tt td:nth-child(1) { text-align: left; width: 62%; }
table.tt td:nth-child(2) { text-align: right; width: 38%; white-space: nowrap; }
table.tt .gt td { font-size: 12px; font-weight: 900; padding: 3px 0; border-top: 1px solid #000; }
.fm { text-align: center; margin-top: 4px; font-size: 8px; color: #000; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
.tel { text-align: center; margin-top: 2px; font-size: 11px; font-weight: 900; letter-spacing: 1px; color: #000; }
</style>
<div class="r-header"><h1>Luuv Fryxo</h1><h2>Receipt</h2><div class="sub">Dambulla</div></div>
<div class="i-row"><span class="lb">Bill</span><span class="vl">{{ doc.name }}</span></div>
<div class="i-row"><span class="lb">Date</span><span class="vl">{{ frappe.utils.format_datetime(doc.creation, "dd MMM, hh:mm a") }}</span></div>
{% if doc.restaurant_table %}<div class="i-row"><span class="lb">Table</span><span class="vl">{{ doc.restaurant_table }}</span></div>{% endif %}
<div class="i-row"><span class="lb">Customer</span><span class="vl">{{ doc.customer_name or "Walk-in" }}</span></div>
<div class="sec"></div>
''' + _PF_BEV_JINJA + '''
{%- set S = namespace(food=[], bev=[], ta=[]) -%}
{%- for item in doc.items -%}
  {%- if item.get("takeaway") -%}
    {%- set S.ta = S.ta + [item] -%}
  {%- else -%}
    {%- set grp = (frappe.db.get_value("Item", item.item_code, "item_group") or "") | lower -%}
    {%- set nm = (item.item_name or item.item_code) | lower -%}
    {%- set B = namespace(v=false) -%}
    {%- for w in BG -%}{%- if w in grp -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- for w in BN -%}{%- if w in nm -%}{%- set B.v = true -%}{%- endif -%}{%- endfor -%}
    {%- if B.v -%}{%- set S.bev = S.bev + [item] -%}{%- else -%}{%- set S.food = S.food + [item] -%}{%- endif -%}
  {%- endif -%}
{%- endfor -%}
{%- set multi = ((1 if S.food else 0) + (1 if S.bev else 0) + (1 if S.ta else 0)) > 1 -%}
<table class="it"><thead><tr><th>Item</th><th>Amount</th></tr></thead>
<tbody>
{%- for sec_title, sec_items in [("FOOD", S.food), ("BEVERAGES", S.bev), ("TAKEAWAY", S.ta)] -%}
  {%- if sec_items -%}
    {%- if multi -%}<tr><td colspan="2" class="sect">&mdash; {{ sec_title }} &mdash;</td></tr>{%- endif -%}
    {%- for item in sec_items -%}
<tr><td>{% if item.get("takeaway") %}(takeaway) {% endif %}{{ item.item_name or item.item_code }} X{{ item.qty|int }}</td><td>Rs.{{ frappe.utils.fmt_money(item.amount, 0) }}</td></tr>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
</tbody></table>
<div class="sec"></div>
<table class="tt"><tbody>
<tr><td>Subtotal</td><td>Rs.{{ frappe.utils.fmt_money(doc.total, 0) }}</td></tr>
{%- for t in doc.taxes -%}
<tr><td>{{ t.description }}</td><td>Rs.{{ frappe.utils.fmt_money(t.tax_amount, 0) }}</td></tr>
{%- endfor -%}
{%- if doc.discount_amount -%}
<tr><td>Discount</td><td>Rs.{{ frappe.utils.fmt_money(doc.discount_amount, 0) }}</td></tr>
{%- endif -%}
<tr class="gt"><td>Total</td><td>Rs.{{ frappe.utils.fmt_money(doc.grand_total, 0) }}</td></tr>
</tbody></table>
<table class="tt"><tbody>
{%- for p in doc.payments -%}
<tr><td>{{ p.mode_of_payment }}</td><td>Rs.{{ frappe.utils.fmt_money(p.amount, 0) }}</td></tr>
{%- endfor -%}
{%- if doc.get("custom_change_returned") and doc.get("custom_change_returned") > 0 -%}
<tr><td>Tendered</td><td>Rs.{{ frappe.utils.fmt_money(doc.get("custom_tendered_amount") or 0, 0) }}</td></tr>
<tr><td>Balance</td><td>Rs.{{ frappe.utils.fmt_money(doc.get("custom_change_returned"), 0) }}</td></tr>
{%- endif -%}
</tbody></table>
<div class="sec"></div>
<div class="fm">Thank you for your visit!</div>
<div class="tel">077 8 927 927</div>
'''


@frappe.whitelist()
def setup_print_sections():
    """Push the section-aware thermal Print Formats into the DB docs (the
    primary print path). Idempotent — overwrites html on each run.
    Run: bench --site <site> execute luuvcrm.api.setup_print_sections"""
    updated, missing = [], []
    for name, html in (("Luuv Fryxo KOT", KOT_PF_HTML),
                       ("Guest Check", GUEST_CHECK_PF_HTML),
                       ("Luuv Fryxo Receipt", RECEIPT_PF_HTML)):
        if frappe.db.exists("Print Format", name):
            frappe.db.set_value("Print Format", name, "html", html)
            updated.append(name)
        else:
            missing.append(name)
    frappe.db.commit()
    return {"updated": updated, "missing": missing}


# ─── Wastage log + food-cost report + approval comments ─────────────────────
# Wastage = spoiled/discarded raw materials, costed from the buying price book
# (same source as /purchases). No recipe/BOM data exists, so "meal cost" is a
# period-level food-cost-% (purchases + wastage vs POS revenue), not per-dish.

@frappe.whitelist()
def setup_wastage():
    """One-time: Wastage DocType + POS Approval.manager_comment field.
    Run: bench --site <site> execute luuvcrm.api.setup_wastage"""
    created = []
    if not frappe.db.exists("DocType", "Wastage"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Wastage", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
                {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data"},
                {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "reqd": 1, "in_list_view": 1},
                {"fieldname": "uom", "label": "Unit", "fieldtype": "Data"},
                {"fieldname": "cost", "label": "Cost", "fieldtype": "Currency", "in_list_view": 1},
                {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
                {"fieldname": "recorded_by", "label": "Recorded By", "fieldtype": "Link", "options": "User"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1, "write": 1, "create": 1},
            ],
        }).insert()
        created.append("Wastage")
    if frappe.db.exists("DocType", "POS Approval"):
        dt = frappe.get_doc("DocType", "POS Approval")
        if not any(f.fieldname == "manager_comment" for f in dt.fields):
            dt.append("fields", {"fieldname": "manager_comment", "label": "Manager Comment",
                                 "fieldtype": "Small Text"})
            dt.flags.ignore_permissions = True
            dt.save()
            created.append("POS Approval.manager_comment")
    return {"status": "created" if created else "already_exists", "created": created}


@frappe.whitelist(methods=["POST"])
def create_wastage(item=None, qty=None, reason=""):
    _require_pos_role()
    if not frappe.db.exists("DocType", "Wastage"):
        setup_wastage()
    item = (item or "").strip()
    if not item or not frappe.db.exists("Item", item):
        frappe.throw(_("Pick a valid item"))
    q = float(qty or 0)
    if q <= 0:
        frappe.throw(_("Quantity must be above zero"))
    d = frappe.db.get_value("Item", item, ["item_name", "stock_uom"], as_dict=True) or {}
    price = float(frappe.db.get_value("Item Price",
        {"item_code": item, "price_list": _buying_price_list()}, "price_list_rate") or 0)
    doc = frappe.get_doc({
        "doctype": "Wastage", "item": item, "item_name": d.get("item_name") or item,
        "qty": q, "uom": d.get("stock_uom") or "Nos", "cost": round(q * price, 2),
        "reason": (reason or "").strip(), "recorded_by": frappe.session.user,
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": doc.name, "cost": float(doc.cost or 0),
            "priced": price > 0}


@frappe.whitelist()
def get_wastage(days=7):
    _require_pos_role()
    if not frappe.db.exists("DocType", "Wastage"):
        return {"rows": [], "total": 0, "days": int(days or 7)}
    days = max(1, min(int(days or 7), 90))
    start = frappe.utils.add_days(frappe.utils.today(), -days + 1)
    rows = frappe.get_all("Wastage", filters={"creation": [">=", start]},
        fields=["name", "item", "item_name", "qty", "uom", "cost", "reason",
                "recorded_by", "creation"],
        order_by="creation desc", limit=100)
    total = 0.0
    for r in rows:
        r["cost"] = float(r["cost"] or 0)
        total += r["cost"]
        r["time"] = frappe.utils.pretty_date(r["creation"])
        r["by"] = frappe.get_value("User", r["recorded_by"], "full_name") or r["recorded_by"] or ""
        r["creation"] = str(r["creation"])[:16]
    return {"rows": rows, "total": round(total, 2), "days": days}


@frappe.whitelist()
def get_food_cost_report(days=7):
    """Sales vs costs for the range: food cost (approved purchase bills +
    wastage), overheads (approved Expenses — rent/salaries/utilities etc.) and
    the resulting profit, all against POS revenue. Period-level (no recipes/BOM
    → no per-dish costing)."""
    _require_menu_manager()
    days = max(1, min(int(days or 7), 365))
    start = frappe.utils.add_days(frappe.utils.today(), -days + 1)
    purchases = 0.0
    if frappe.db.exists("DocType", "Purchase Bill"):
        purchases = float(frappe.db.sql("""
            select coalesce(sum(total), 0) from `tabPurchase Bill`
            where status = 'approved' and requested_at >= %s
        """, (start,))[0][0] or 0)
    wastage = 0.0
    if frappe.db.exists("DocType", "Wastage"):
        wastage = float(frappe.db.sql("""
            select coalesce(sum(cost), 0) from `tabWastage`
            where creation >= %s
        """, (start,))[0][0] or 0)
    overhead = 0.0
    if frappe.db.exists("DocType", "Expense"):
        overhead = float(frappe.db.sql("""
            select coalesce(sum(amount), 0) from `tabExpense`
            where status = 'approved'
              and coalesce(expense_date, date(creation)) >= %s
        """, (start,))[0][0] or 0)
    revenue = float(frappe.db.sql("""
        select coalesce(sum(grand_total), 0) from `tabPOS Invoice`
        where docstatus = 1 and posting_date >= %s
    """, (start,))[0][0] or 0)
    food_cost = round(purchases + wastage, 2)
    profit = round(revenue - food_cost - overhead, 2)
    pct = lambda v: round(v * 100.0 / revenue, 1) if revenue > 0 else 0
    return {"days": days,
            "purchases": round(purchases, 2), "wastage": round(wastage, 2),
            "food_cost": food_cost, "overhead": round(overhead, 2),
            "profit": profit, "revenue": round(revenue, 2),
            "food_cost_pct": pct(food_cost), "wastage_pct": pct(wastage),
            "overhead_pct": pct(overhead), "profit_pct": pct(profit)}


@frappe.whitelist(methods=["POST"])
def reset_guide_password(guide=None):
    """Issue a fresh magic-link login for the guide (old links stop working) and
    return the wa.me re-invite text. Name kept for the manager UI's old call."""
    _require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    frappe.db.delete("Guide Token", {"guide": g.name})
    token = _get_or_create_guide_token(g.name)
    frappe.db.commit()
    return {"ok": True, "whatsapp": g.whatsapp,
            "invite_text": _guide_invite_text(token,
                float(g.cashback_percent or 0), g.guide_name or "")}


# Appended 2026-09-02: standalone helper for the manager's per-day purchase
# grouping. Server api.py is the canonical copy (it is AHEAD of the repo);
# this was added surgically, not by replacing the file.
@frappe.whitelist()
def get_approval_days():
    """Calendar date (and Purchase Bill) for each pending approval, keyed by name.

    The manager groups purchase bills into one card per day, but a deployed
    _format_approvals() may predate the date/date_label/bill fields it reads.
    This stands alone so it can be dropped into any build without touching that
    function; the frontend only calls it when the fields are missing.
    """
    if not frappe.db.exists("DocType", "POS Approval"):
        return {"days": {}}
    rows = frappe.get_all("POS Approval", filters={"status": "pending"},
                          fields=["name", "requested_at", "action", "action_payload"])
    days = {}
    for r in rows:
        bill = ""
        if (r.get("action") or "") == "purchase_bill":
            try:
                bill = (json.loads(r.get("action_payload") or "{}") or {}).get("bill") or ""
            except Exception:
                bill = ""
        days[r.name] = {
            "date": str(r.get("requested_at") or "")[:10],
            "date_label": (frappe.utils.formatdate(r.get("requested_at"), "EEE d MMM")
                           if r.get("requested_at") else ""),
            "bill": bill,
        }
    return {"days": days}


# Appended 2026-09-02 (surgical — server api.py is canonical, see AGENTS.md):
# ~200-byte heartbeat for the manager's 8s approvals poll. The full
# get_pending_approvals list only travels when this signature changes.
@frappe.whitelist()
def get_approvals_digest():
    """Pending count + a change signature for POS Approval polling.

    sig = latest `modified` across ALL approvals (a resolve bumps modified, so
    approvals leaving the pending list move the signature too) + the pending
    count, so a create+resolve inside one poll window cannot alias.
    """
    if not frappe.db.exists("DocType", "POS Approval"):
        return {"pending": 0, "sig": "none"}
    row = frappe.db.sql(
        """select count(case when status='pending' then 1 end), max(modified)
           from `tabPOS Approval`""")[0]
    pending = int(row[0] or 0)
    return {"pending": pending, "sig": f"{row[1]}|{pending}"}
