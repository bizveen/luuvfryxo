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
    """POS Invoice (native pipeline) when an open shift exists for the profile, else
    Sales Invoice. POS Invoice requires an open POS Opening Entry; falling back keeps
    no-shift flows (rare edge) from hard-failing."""
    if pos_profile_name and frappe.db.exists("POS Opening Entry",
            {"pos_profile": pos_profile_name, "status": "Open", "docstatus": 1}):
        return "POS Invoice"
    return "Sales Invoice"

def _get_invoice_doc(invoice_name):
    """Load an order's invoice regardless of whether it is a POS Invoice or Sales Invoice."""
    dt = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
    return frappe.get_doc(dt, invoice_name)

def _ensure_service_charge_item():
    if not frappe.db.exists("Item", "Service Charge"):
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": "Service Charge",
            "item_name": "Service Charge",
            "item_group": "Services",
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "is_sales_item": 1,
        })
        item.flags.ignore_permissions = True
        item.insert()

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
    _ensure_service_charge_item()

    invoice_items = []
    pos_items = []
    subtotal = 0

    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())

        invoice_items.append({"item_code": item_code, "qty": qty, "rate": rate})
        pos_items.append({"item": item_code, "qty": qty, "rate": rate})
        subtotal += rate * qty

    sc_rate = _get_service_charge_rate()
    sc_amount = _calc_service_charge(subtotal)
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
        invoice.append("items", {"item_code": "Service Charge", "qty": 1, "rate": sc_amount})

    invoice.flags.ignore_permissions = True
    invoice.insert()

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
        })

    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)

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
        "notes": notes,
        "items": [],
    })

    opening_entry_name = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")
    if opening_entry_name:
        pos_order.pos_opening_entry = opening_entry_name

    subtotal = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate})
        subtotal += rate * qty

    sc_rate = _get_service_charge_rate()
    sc_amount = _calc_service_charge(subtotal)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)

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

    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))

    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)

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
        invoice.append("items", {"item_code": item.item, "qty": item.qty, "rate": item.rate})

    if pos_order.service_charge_amount and pos_order.service_charge_amount > 0:
        _ensure_service_charge_item()
        invoice.append("items", {"item_code": "Service Charge", "qty": 1, "rate": pos_order.service_charge_amount})

    invoice.flags.ignore_permissions = True
    invoice.insert()

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

# ─── Ongoing Orders ──────────────────────────────────────

@frappe.whitelist()
def get_ongoing_orders():
    orders = frappe.get_all("POS Order",
        filters={"docstatus": 0, "order_source": ["in", ["Walk-in", "Waiter"]]},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice", "order_type"],
        order_by="creation desc"
    )
    # Exclude orders that already have an invoice (already paid)
    orders = [o for o in orders if not o.pos_invoice]
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        doc = frappe.get_doc("POS Order", o.name)
        o["items_count"] = len(doc.get("items") or [])
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])])
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
        o["items_json"] = json.dumps([{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])])
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
        filters={"ref_doctype": "POS Order"},
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

@frappe.whitelist(methods=["POST"])
def pos_open_shift():
    _require_pos_role()
    data = frappe.local.form_dict
    pos_profile_name = _resolve_pos_profile_name(data.get("pos_profile", ""))
    opening_balance = float(data.get("opening_balance", 0))

    existing = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user}, "name")
    if existing:
        frappe.throw(_("A shift is already open. Close it first."))

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)

    opening = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "pos_profile": pos_profile_name,
        "company": pos_profile.company,
        "period_start_date": now_datetime(),
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "user": frappe.session.user,
        "balance_details": [{
            "mode_of_payment": "Cash",
            "opening_amount": opening_balance
        }]
    })
    opening.insert()
    opening.submit()

    return {
        "shift": {
            "status": "open",
            "name": opening.name,
            "opening_balance": opening_balance,
            "total_sales": 0,
            "order_count": 0,
            "payment_breakdown": {"Cash": 0},
            "period_start": str(opening.period_start_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }

@frappe.whitelist(methods=["POST"])
def pos_close_shift():
    _require_pos_role()
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    data = frappe.local.form_dict
    closing_balance = float(data.get("closing_balance", 0))

    opening_name = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": frappe.session.user}, "name")
    if not opening_name:
        frappe.throw(_("No open shift found"))

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    # Native: gather this shift's submitted POS Invoices into a POS Closing Entry
    # (pos_transactions + taxes + payment reconciliation built by ERPNext).
    closing = make_closing_entry_from_opening(opening)

    # Apply the cashier's counted cash; trust expected for non-cash modes.
    payment_breakdown = {}
    for row in closing.payment_reconciliation:
        expected = float(row.expected_amount or 0)
        opening_amt_row = float(row.opening_amount or 0)
        row.closing_amount = closing_balance if row.mode_of_payment == "Cash" else expected
        row.difference = float(row.closing_amount) - expected
        payment_breakdown[row.mode_of_payment] = expected - opening_amt_row

    closing.flags.ignore_permissions = True
    closing.insert()
    closing.submit()  # triggers native consolidation -> Sales Invoice(s)

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


@frappe.whitelist()
def get_pos_shift():
    _require_pos_role()
    opening_name = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "user": frappe.session.user}, "name")
    if not opening_name:
        return {"shift": None}

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    opening_amt = 0
    for d in opening.balance_details:
        opening_amt += float(d.opening_amount)

    orders = frappe.get_all("POS Order",
        filters={"pos_opening_entry": opening_name, "docstatus": 1},
        fields=["grand_total", "name"])
    total_sales = sum(o.grand_total for o in orders)
    order_count = len(orders)

    payment_breakdown = {}
    if orders:
        payments = frappe.db.sql("""
            SELECT sip.mode_of_payment, SUM(sip.amount) as total
            FROM `tabSales Invoice Payment` sip
            INNER JOIN `tabPOS Invoice` si ON si.name = sip.parent
            INNER JOIN `tabPOS Order` po ON po.pos_invoice = si.name
            WHERE po.pos_opening_entry = %s AND po.docstatus = 1
              AND sip.parenttype = 'POS Invoice'
            GROUP BY sip.mode_of_payment
        """, opening_name, as_dict=True)
        for p in payments:
            payment_breakdown[p.mode_of_payment] = float(p.total)

    return {
        "shift": {
            "status": "open",
            "name": opening_name,
            "opening_balance": opening_amt,
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

# ─── Order Items Detail ──────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_order_items(order_name):
    if not order_name:
        return {"items": []}
    doc = frappe.get_doc("POS Order", order_name)
    items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate} for i in (doc.get("items") or [])]
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
                "pos_invoice"],
        order_by="creation asc"
    )
    # Exclude paid orders (have invoice)
    orders = [o for o in orders if not o.pos_invoice]
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        doc = frappe.get_doc("POS Order", o.name)
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate}
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
        filters={"kitchen_status": ["not in", ["Served"]]},
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

    # Update items
    pos_order.items = []
    subtotal = 0
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate})
        subtotal += rate * qty

    sc_rate = _get_service_charge_rate()
    sc_amount = _calc_service_charge(subtotal)
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
                invoice.append("items", {"item_code": item_code, "qty": qty, "rate": rate})

            if sc_amount > 0:
                _ensure_service_charge_item()
                invoice.append("items", {"item_code": "Service Charge", "qty": 1, "rate": sc_amount})

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
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = float(item_data.get("rate", 0))
        if not rate:
            rate = _resolve_item_rate(item_code, 0, _get_pos_price_list())
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        pos_order.append("items", {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate})
        subtotal += rate * qty

    sc_rate = _get_service_charge_rate()
    sc_amount = _calc_service_charge(subtotal)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
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
                rate_map[code] = {"qty": qty, "rate": rate}

            invoice_items = []
            subtotal = 0
            for code, ri in rate_map.items():
                invoice_items.append({"item_code": code, "qty": ri["qty"], "rate": ri["rate"]})
                subtotal += ri["rate"] * ri["qty"]

            sc_rate = _get_service_charge_rate()
            sc_amount = _calc_service_charge(subtotal)
            grand_total = subtotal + sc_amount

            payments = [{"mode_of_payment": payment_mode, "amount": grand_total}]
            if payment_mode == "Cash+Card":
                payments = []
                if cash_amount > 0:
                    payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
                if card_amount > 0:
                    payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})

            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)

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
                _ensure_service_charge_item()
                invoice.append("items", {"item_code": "Service Charge", "qty": 1, "rate": sc_amount})

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
                pos_order.append("items", {"item": code, "item_name": item_name, "qty": ri["qty"], "rate": ri["rate"]})
            pos_order.flags.ignore_permissions = True
            pos_order.flags.ignore_links = True
            pos_order.insert()

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
        items.append({
            "item": i.item,
            "item_name": i.item_name or i.item,
            "qty": i.qty,
        })

    return {
        "order_name": doc.name,
        "table": doc.restaurant_table or "",
        "waiter_name": doc.waiter_name or "",
        "order_source": doc.order_source or "Walk-in",
        "notes": doc.notes or "",
        "items": items,
        "creation": str(doc.creation),
    }


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

    track_url = f"https://luuvgrand.com/myorders?phone={mobile}"
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