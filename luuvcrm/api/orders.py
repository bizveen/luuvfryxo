"""An order's life: taken, sent to the kitchen, amended, listed, moved, cancelled.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import hashlib
import hmac
from urllib.parse import quote

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import roles

from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.schema import (
    _clean_item_note,
    _ensure_item_notes_field,
    _install_sent_qty_field,
)
from luuvcrm.api.shifts import _append_service_charge_tax, _get_invoice_doc


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
#: Ceilings for the polled screens. `get_all`/`get_list` without one reads the
#: whole table, and every one of these filters on a *status* rather than a date —
#: so one order stuck in Draft or unpaid is a row that is read again on every
#: poll, for ever. The numbers are far above a real service; they are a ceiling,
#: not a page size, and hitting one means something is stuck, not busy.
OPEN_ORDER_CEILING = 500      # open orders across every table
SCREEN_ORDER_CEILING = 200    # one screen's worth: ongoing, kiosk, kitchen


@frappe.whitelist()
def get_tables_with_status():
    active = frappe.get_list("POS Order",
        filters=[
            ["docstatus", "=", 0],
            ["kitchen_status", "!=", "Served"],
            ["pos_invoice", "is", "not set"],
        ],
        fields=["restaurant_table", "name", "kitchen_status"],
        # Deterministic: a table with several open orders binds its tile to the
        # OLDEST one (matches get_table_orders); without this the pick is arbitrary.
        order_by="creation asc",
        limit_page_length=OPEN_ORDER_CEILING,
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
        for dup_name in frappe.get_list("POS Order", filters={
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
        rate = _server_item_rate(item_code)
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
# ─── Ongoing Orders ──────────────────────────────────────

@frappe.whitelist()
def get_ongoing_orders():
    orders = frappe.get_list("POS Order",
        filters={"docstatus": 0, "order_source": ["in", ["Walk-in", "Waiter"]]},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice", "order_type",
                "discount_amount", "discount_note"],
        order_by="creation desc",
        limit_page_length=SCREEN_ORDER_CEILING,
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
    orders = frappe.get_list("POS Order",
        filters={"docstatus": 0, "order_source": "Online"},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice"],
        order_by="creation desc",
        limit_page_length=SCREEN_ORDER_CEILING,
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

    orders = frappe.get_list("POS Order",
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
@frappe.whitelist(methods=["POST"])
def mark_kot_printed():
    """Flag that the current items have been fired to the kitchen — sets each line's
    sent_qty to its qty, so the next KOT shows only items added afterwards."""
    # Any authenticated user (incl. the Waiter role, which is NOT in roles.POS_ROLES) may fire a KOT —
    # this only advances sent_qty on an order (benign, same Draft→Pending promotion send_to_kitchen
    # already does gate-free). Whitelist alone blocks guests.
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        _install_sent_qty_field()
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
    frappe.only_for("System Manager")
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
    roles.require_pos()
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

def track_signature(order_name):
    """The proof that whoever holds this link was given it.

    Keyed on the site's own encryption key, so it cannot be produced from the
    order name alone and is different on every site. Truncated to 32 hex
    characters: 128 bits is far past what a link in a chat message needs, and a
    shorter link is one people will actually paste.
    """
    from frappe.utils.password import get_encryption_key

    return hmac.new(get_encryption_key().encode(),
                    str(order_name).encode(), hashlib.sha256).hexdigest()[:32]


def track_url(order_name):
    """The link to give a customer for one order. Signed."""
    from luuvcrm import settings

    return settings.url(f"/track?order={quote(str(order_name))}"
                        f"&s={track_signature(order_name)}")


@frappe.whitelist(allow_guest=True)
def get_order_items(order_name, s=None):
    """Lines on an order.

    Guest-readable, because /track is a genuinely public page: the customer
    arrives holding a link and nothing else. What a guest gets is trimmed to
    what a receipt shows — the billing and kitchen-progress counters (paid_qty,
    sent_qty, remaining) and the kitchen notes are staff data and go only to a
    signed-in user.

    **S7b.** A guest also has to present the signature. Order names are a
    guessable sequence (`POS-00104`), so a bare name made this an enumeration
    surface: walk the numbers and read what every table ate, along with the
    customer name on the order. The signature is what turns "knows the format"
    into "was given this link".

    Nothing in the wild breaks. This was deferred on the grounds that it changes
    every WhatsApp link already in customers' hands — but no message sends a
    /track link at all. The one place that builds a customer-facing tracking
    link sends them to /cus, and /admin opens /track as staff, who are exempt.
    """
    if not order_name:
        return {"items": []}
    staff = frappe.session.user != "Guest"
    if not staff and not hmac.compare_digest(str(s or ""), track_signature(order_name)):
        # Deliberately the same answer as an order that does not exist: telling
        # the difference is most of what an enumerator wants.
        return {"items": []}
    doc = frappe.get_doc("POS Order", order_name)
    items = []
    for i in (doc.get("items") or []):
        row = {"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate,
               "takeaway": int(i.get("takeaway") or 0)}
        if staff:
            row.update({"paid_qty": int(i.get("paid_qty") or 0),
                        "sent_qty": int(i.get("sent_qty") or 0),
                        "notes": i.get("notes") or "",
                        "remaining": int(i.qty or 0) - int(i.get("paid_qty") or 0)})
        items.append(row)
    return {"items": items}
# ─── Orders by Table ─────────────────────────────────────

@frappe.whitelist()
def get_table_orders():
    table = frappe.local.form_dict.get("table", "")
    if not table:
        return {"orders": []}
    orders = frappe.get_list("POS Order",
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
    orders = frappe.get_list("POS Order",
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
        _install_sent_qty_field()
    # Whatever is already on the bill is considered already fired to the kitchen — so the KOT for
    # this append prints ONLY the new lines (get_kot_print_data keys on qty - sent_qty).
    for i in (pos_order.get("items") or []):
        i.sent_qty = int(i.qty or 0)

    order_type = pos_order.order_type or ""
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = _server_item_rate(item_code)
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
# ─── Edit Order ─────────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def update_order():
    """Rebuild a draft order's item lines. POS staff only.

    This was allow_guest=True. Since POS Order names are a guessable sequence,
    that let anyone on the internet rewrite any open bill's items, quantities,
    customer and phone — and the rebuild is then pushed into the linked invoice
    below. Only /pos calls this, and it is behind a
    login, so the guard costs nothing.

    The save below runs under the caller's own permissions. It used to pass
    ignore_permissions because POS Order granted write to every role except
    Cashier — the one role that actually works the till. That gap is fixed in
    the doctype's DocPerms, so the flag is gone and Frappe now checks the edit.

    The linked-invoice save further down still passes it: POS Invoice belongs to
    ERPNext and its perms are granted at runtime by setup_pos_shift_perms, which
    is itself on the list to be replaced (ROADMAP Phase 3).
    """
    roles.require_pos()
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
    # "order-table-mismatch" is matched by pos.html to reset + refresh.
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
    # Also preserve each line's per-item note (the POS doesn't send notes back, so without this a
    # cashier autosave would wipe the waiter's kitchen instructions). Distinct notes for the same
    # (item, takeaway) group are joined, since the POS folds them into a single cart line.
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
        rate = _server_item_rate(item_code)
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
                rate = _server_item_rate(item_code)
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
@frappe.whitelist()
def get_order_changelog(order_name):
    """Edit history of an order. Staff only — it names the user behind each edit,
    so it was never something to hand a guest. Only /pos calls it."""
    roles.require_pos()
    if not order_name:
        return {"changelog": []}

    # Fetch versions (Frappe's built-in version tracking)
    versions = frappe.get_all("Version",
        filters={"ref_doctype": "POS Order", "docname": order_name},
        fields=["name", "creation", "owner", "data"],
        order_by="creation asc",
        limit_page_length=200,
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
