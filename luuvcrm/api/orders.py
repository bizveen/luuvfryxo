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
from luuvcrm import activity, branch, roles
from luuvcrm.api import tables
from luuvcrm.api.pos_inventory import FIELD as INVENTORY_MODE, creation_mode, list_filter

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
from luuvcrm.api.shifts import (
    _active_open_shift_name,
    _append_service_charge_tax,
    _get_invoice_doc,
)


# ─── POS Profiles ────────────────────────────────────────

@frappe.whitelist()
def get_pos_profiles():
    roles.require_pos()
    names = branch.accessible_pos_profiles()
    profiles = frappe.get_all("POS Profile", filters={"name": ["in", names]},
                              fields=["name", "company", "warehouse", "currency"],
                              order_by="name asc") if names else []
    modes = frappe.get_all("Mode of Payment", fields=["name"], order_by="name asc")
    return {"profiles": profiles, "payment_modes": modes}
# ─── Tables ──────────────────────────────────────────────

@frappe.whitelist()
def get_tables():
    # One reader for every picker: this branch's tables, the ones in service.
    return {"tables": tables.for_picker()}
#: Ceilings for the polled screens. `get_all`/`get_list` without one reads the
#: whole table, and every one of these filters on a *status* rather than a date —
#: so one order stuck in Draft or unpaid is a row that is read again on every
#: poll, for ever. The numbers are far above a real service; they are a ceiling,
#: not a page size, and hitting one means something is stuck, not busy.
OPEN_ORDER_CEILING = 500      # open orders across every table
SCREEN_ORDER_CEILING = 200    # one screen's worth: ongoing, kiosk, kitchen


#: The line columns every order screen reads. `takeaway`, `sent_qty` and `notes`
#: are custom fields (setup_doctype), so a site mid-install may not have them —
#: asked for by name, they would turn a poll into an exception.
_LINE_FIELDS = ["item", "item_name", "qty", "rate"]
_LINE_OPTIONAL = ["takeaway", "sent_qty", "notes"]


def _is_cus_call_pending(order):
    """CUS tickets stay held until POS staff explicitly confirm the call."""
    return (order.get("order_source") == "Online"
            and str(order.get("waiter_name") or "").strip().upper() == "CUS"
            and order.get("kitchen_status") == "Draft")


def _require_cus_call_released(order):
    if _is_cus_call_pending(order):
        frappe.throw(_("Confirm the APP order before continuing."))


@frappe.whitelist(methods=["POST"])
def confirm_app_order():
    """Accept one held APP order without firing its kitchen ticket.

    Confirmation is deliberately separate from KOT acknowledgement: the app
    sees Draft -> Pending immediately, while sent_qty and inventory move only
    when staff subsequently press KOT.
    """
    roles.require_pos()
    order_name = str(frappe.local.form_dict.get("order_name") or "").strip()
    if not order_name:
        frappe.throw(_("Order name is required"))
    branch.require_access("POS Order", order_name)
    order = frappe.get_doc("POS Order", order_name, for_update=True)
    is_app = (order.get("order_source") == "Online"
              and str(order.get("waiter_name") or "").strip().upper() == "CUS")
    if not is_app:
        frappe.throw(_("Only APP orders can be confirmed here."))
    if order.kitchen_status in ("Served", "Cancelled"):
        frappe.throw(_("This order is already closed."))

    confirmed = order.kitchen_status == "Draft"
    push_queued = False
    if confirmed:
        frappe.db.set_value("POS Order", order_name, "kitchen_status", "Pending")
        # Make acceptance durable before the best-effort customer notification.
        # Referral cashback is awarded only after payment.
        frappe.db.commit()
        try:
            from luuvcrm.api.push import queue_order_confirmed_push

            push_queued = queue_order_confirmed_push(order_name)
        except Exception as exc:
            # Confirmation has already committed; notification delivery is
            # best effort and must never turn a successful POS tap into an
            # error response or invite a duplicate confirmation.
            frappe.log_error(
                f"APP confirmation push queue error: {type(exc).__name__}",
                "Customer push notification failed",
            )
    return {
        "ok": True,
        "name": order_name,
        "status": "Pending" if confirmed else order.kitchen_status,
        "confirmed": confirmed,
        "push_queued": push_queued,
    }


def _mobile_key(mobile):
    """Comparable local form for customer-account/order mobile numbers."""
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    if digits.startswith("94") and len(digits) == 11:
        return "0" + digits[2:]
    return digits


def _tag_registered_customers(orders):
    """Mark orders whose phone belongs to an OTP-registered Customer Account.

    One lookup covers the whole POS list; this screen polls, so checking each
    order separately would turn a small UI badge into an N+1 query.
    """
    keys = {_mobile_key(o.get("mobile")) for o in orders if o.get("mobile")}
    keys.discard("")
    registered = set()
    if keys and frappe.db.exists("DocType", "Customer Account"):
        variants = set(keys)
        variants.update("94" + key[1:] for key in keys if key.startswith("0") and len(key) == 10)
        variants.update("+94" + key[1:] for key in keys if key.startswith("0") and len(key) == 10)
        accounts = frappe.get_all("Customer Account", filters={"mobile": ["in", list(variants)]},
                                  fields=["mobile"], limit_page_length=0)
        registered = {_mobile_key(account.mobile) for account in accounts}
    for order in orders:
        order["customer_registered"] = bool(_mobile_key(order.get("mobile")) in registered)
    return orders


def lines_by_order(names):
    """Every line of many orders, in one read, keyed by order name.

    P1. Each of the polled screens built a full POS Order document per row to
    reach `doc.items` — twenty open orders was twenty documents, every ten
    seconds, per device. The lines are a child table: one query answers for all
    of them, in `idx` order so the kitchen ticket still reads the way it was
    entered.
    """
    if not names:
        return {}
    fields = ["parent", "idx"] + _LINE_FIELDS + [
        c for c in _LINE_OPTIONAL if frappe.db.has_column("POS Order Item", c)]
    out = {}
    for r in frappe.get_all("POS Order Item", filters={"parent": ["in", list(names)]},
                            fields=fields, order_by="parent asc, idx asc",
                            limit_page_length=0):
        out.setdefault(r.parent, []).append(r)
    return out


def _line_dict(i, extra=True):
    """One line as the order screens send it."""
    d = {"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate}
    if extra:
        d.update({"takeaway": int(i.get("takeaway") or 0),
                  "sent_qty": int(i.get("sent_qty") or 0),
                  "notes": i.get("notes") or ""})
    return d


@frappe.whitelist()
def get_tables_with_status():
    active = frappe.get_list("POS Order",
        filters=[
            ["docstatus", "=", 0],
            ["kitchen_status", "!=", "Served"],
            ["pos_invoice", "is", "not set"],
            [branch.FIELD, "=", branch.viewing()],
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
    roles.require_waiter()
    data = frappe.local.form_dict
    from luuvcrm.api.pos_customers import prepare_pos_customer
    from luuvcrm.customer_phone import normalize_mobile

    customer_fields = prepare_pos_customer(data)
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Walk-in"
    mobile = customer_fields.get("mobile", data.get("mobile", "").strip() or "")
    country_code = (data.get("country_code") or "94").strip() or "94"   # dial code for the WhatsApp number
    table = data.get("table", "")
    if table:
        branch.require_access("Restaurant Table", table)
    notes = data.get("notes", "").strip()
    order_source = data.get("order_source", "Walk-in")
    order_type = data.get("order_type", "")
    amended_from = data.get("amended_from", "").strip()
    if amended_from:
        branch.require_access("POS Order", amended_from)
    waiter_name = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    # Draft = auto-created as the cashier builds the ticket; not yet fired to the kitchen.
    is_draft = int(data.get("draft") or 0)
    inventory_mode = creation_mode(amended_from)

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
                INVENTORY_MODE: inventory_mode,
                "owner": frappe.session.user, "pos_invoice": ["in", ["", None]],
                "creation": [">", add_to_date(now_datetime(), seconds=-20)]},
                order_by="creation desc", pluck="name"):
            dup = frappe.get_doc("POS Order", dup_name)
            if any(int(i.get("sent_qty") or 0) for i in (dup.get("items") or [])):
                continue
            existing = sorted((i.item, int(i.get("takeaway") or 0), int(i.qty or 0))
                              for i in (dup.get("items") or []))
            if existing == incoming and (not customer_fields or normalize_mobile(dup.mobile) == mobile):
                if customer_fields:
                    frappe.db.set_value("POS Order", dup.name, customer_fields)
                return {
                    "name": dup.name,
                    "grand_total": dup.grand_total,
                    "subtotal": sum(float(i.rate or 0) * int(i.qty or 0) for i in dup.items),
                    "service_charge_rate": dup.service_charge_rate,
                    "service_charge_amount": dup.service_charge_amount,
                    "table": table,
                    "customer_name": dup.customer_name,
                    "mobile": mobile,
                    "deduped": True,
                }

    # A waiter serves the table's bill, not a private ticket belonging to one
    # device or one staff member.  Lock the table while resolving its open bill
    # so two waiters sending at the same time cannot both create a second order.
    # POS-created orders count too: floor staff must be able to add a customer's
    # later request to the same unpaid bill regardless of who opened it.
    if order_source == "Waiter" and table and not is_draft and not amended_from:
        frappe.db.sql(
            "select name from `tabRestaurant Table` where name=%s for update",
            (table,),
        )
        open_orders = frappe.get_list(
            "POS Order",
            filters=[
                ["docstatus", "=", 0],
                ["restaurant_table", "=", table],
                ["pos_invoice", "is", "not set"],
                [branch.FIELD, "=", branch.viewing()],
            ],
            order_by="creation asc",
            pluck="name",
            limit_page_length=2,
        )
        if len(open_orders) > 1:
            frappe.throw(
                _("This table has more than one open order. Select the correct order from Orders.")
            )
        if open_orders:
            return _append_waiter_items(open_orders[0], items)

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        "restaurant_table": table,
        "order_source": order_source,
        "kitchen_status": "Draft" if is_draft else "Pending",
        INVENTORY_MODE: inventory_mode,
        "grand_total": 0,
        "service_charge_rate": 0,
        "service_charge_amount": 0,
        "notes": notes,
        "items": [],
    })

    try:
        opening_entry_name = _active_open_shift_name()
    except Exception:
        # The shift link is optional on an order.  Branch stamping must remain
        # the source of truth even if a broken till assignment cannot resolve.
        opening_entry_name = None
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
    pos_order.update(customer_fields)
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)
    _apply_takeaway_service_charge(pos_order)
    if not is_draft:
        from luuvcrm.api.inventory import consume_order_stock

        consume_order_stock(pos_order.name)

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

    activity.log_activity(
        "order.draft" if is_draft else "order.fire",
        label=("Ticket started Rs %.0f" if is_draft else "Sent to kitchen Rs %.0f") % (
            float(pos_order.grand_total or 0)),
        ref_doctype="POS Order", ref_name=pos_order.name, table=table or None,
        amount=float(pos_order.grand_total or 0),
        total_after=float(pos_order.grand_total or 0),
        detail={"items": len(items), "subtotal": subtotal,
                "service_charge": sc_amount, "order_type": order_type,
                "source": order_source, "amended_from": amended_from or None})

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
        filters={"docstatus": 0, "order_source": ["in", ["Walk-in", "Waiter", "Online"]],
                 "restaurant": branch.viewing(), **list_filter()},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice", "order_type",
                "discount_amount", "discount_note", "waiter_name", "kitchen_status"]
               + [field for field in ("scheduled_time", "whatsapp_status")
                  if frappe.db.has_column("POS Order", field)],
        order_by="creation desc",
        limit_page_length=SCREEN_ORDER_CEILING,
    )
    # Exclude orders that already have an invoice (already paid)
    orders = [o for o in orders if not o.pos_invoice]
    lines = lines_by_order([o.name for o in orders])
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        rows = lines.get(o.name, [])
        o["items_count"] = len(rows)
        o["items_json"] = json.dumps([_line_dict(i) for i in rows])
    _tag_registered_customers(orders)
    return {"orders": orders}
# ─── Kiosk Orders (Online source) ────────────────────────

@frappe.whitelist()
def get_kiosk_orders():
    orders = frappe.get_list("POS Order",
        filters={"docstatus": 0, "order_source": "Online", "restaurant": branch.viewing()},
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "pos_invoice"],
        order_by="creation desc",
        limit_page_length=SCREEN_ORDER_CEILING,
    )
    lines = lines_by_order([o.name for o in orders])
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        rows = lines.get(o.name, [])
        o["items_count"] = len(rows)
        o["items_json"] = json.dumps([_line_dict(i) for i in rows])
    return {"orders": orders}
# ─── Completed Orders (current shift only) ──────────────

@frappe.whitelist()
def get_completed_orders():
    opening_name = _active_open_shift_name()

    filters = {"docstatus": 1, "order_source": ["in", ["Walk-in", "Waiter", "Online"]],
               "restaurant": branch.viewing(), **list_filter()}
    if opening_name:
        filters["pos_opening_entry"] = opening_name

    orders = frappe.get_list("POS Order",
        filters=filters,
        fields=["name", "customer_name", "mobile", "restaurant_table",
                "grand_total", "creation", "modified", "order_source", "pos_invoice", "order_type",
                "kitchen_status", "waiter_name"]
               + [field for field in ("scheduled_time", "whatsapp_status")
                  if frappe.db.has_column("POS Order", field)],
        order_by="modified desc",
        limit_page_length=100
    )
    lines = lines_by_order([o.name for o in orders])
    for o in orders:
        o["completed_at"] = frappe.utils.pretty_date(o["modified"])
        rows = lines.get(o.name, [])
        o["items_count"] = len(rows)
        o["items_json"] = json.dumps([_line_dict(i, extra=False) for i in rows])
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
    _tag_registered_customers(orders)
    return {"orders": orders}
# ─── Cancel Order ────────────────────────────────────────

@frappe.whitelist(methods=["POST"])
def cancel_order():
    roles.require_pos()
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot cancel — payment already processed"))
    old_kitchen_status = pos_order.kitchen_status
    from luuvcrm.api.waiter_delivery import guard_cancel
    guard_cancel(pos_order)
    pos_order.db_set("docstatus", 2)
    pos_order.db_set("kitchen_status", "Cancelled")
    from luuvcrm.api.loyalty import reverse_order_cashback, reverse_reward_redemption
    from luuvcrm.api.offers import reverse_offer_redemption

    reverse_reward_redemption(order_name)
    reverse_order_cashback(order_name)
    reverse_offer_redemption(order_name)
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

    # A cancel after the kitchen has fired is the one worth a manager's eye: the
    # food was cooked and the bill went away.
    fired = sum(int(i.get("sent_qty") or 0) for i in (pos_order.get("items") or []))
    flag, note = activity.kot_pull_flag(fired, "the whole ticket")
    activity.log_activity(
        "order.cancel", label="Cancelled Rs %.0f" % float(pos_order.grand_total or 0),
        ref_doctype="POS Order", ref_name=order_name,
        table=pos_order.restaurant_table,
        amount=float(pos_order.grand_total or 0),
        total_before=float(pos_order.grand_total or 0), total_after=0,
        flag_reason=flag, flag_note=note,
        detail={"was": old_kitchen_status, "items": len(pos_order.get("items") or []),
                "fired_qty": fired})
    return {"status": "cancelled", "name": order_name}
# ─── All Changelogs (recent changes across orders) ──────

@frappe.whitelist()
def get_all_changelogs():
    visible_orders = frappe.get_list("POS Order", filters={
                                         "modified": [">=", frappe.utils.today()],
                                         branch.FIELD: branch.viewing()},
                                     pluck="name", limit_page_length=0)
    if not visible_orders:
        return {"logs": []}
    versions = frappe.get_all("Version",
        filters={"ref_doctype": "POS Order", "docname": ["in", visible_orders],
                 "creation": [">=", frappe.utils.today()]},
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
    roles.require_kitchen()
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))

    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not in draft state"))
    if pos_order.kitchen_status == "Served":
        frappe.throw(_("Order already served"))
    _require_cus_call_released(pos_order)

    if pos_order.pos_invoice:
        invoice = _get_invoice_doc(pos_order.pos_invoice)
        if invoice.docstatus == 0:
            invoice.flags.ignore_permissions = True
            invoice.submit()

    pos_order.db_set("docstatus", 1)
    pos_order.db_set("kitchen_status", "Served")

    activity.log_activity(
        "order.served", label="Served Rs %.0f" % float(pos_order.grand_total or 0),
        ref_doctype="POS Order", ref_name=order_name, bill_no=pos_order.pos_invoice or None,
        table=pos_order.restaurant_table, amount=float(pos_order.grand_total or 0),
        detail={"invoice": pos_order.pos_invoice or None})

    return {"name": order_name, "status": "Served", "invoice_name": pos_order.pos_invoice}
@frappe.whitelist(methods=["POST"])
def mark_kot_printed():
    """Flag that the current items have been fired to the kitchen — sets each line's
    sent_qty to its qty, so the next KOT shows only items added afterwards."""
    # Any authenticated user (incl. the Waiter role, which is NOT in roles.POS_ROLES) may fire a KOT —
    # this only advances sent_qty on an order (benign, same Draft→Pending promotion send_to_kitchen
    # already does gate-free). Whitelist alone blocks guests.
    roles.require_waiter()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    branch.require_access("POS Order", order_name)
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        _install_sent_qty_field()
    # A locking read sees the latest state even when a branch-access query
    # opened an earlier database snapshot.
    doc = frappe.get_doc("POS Order", order_name, for_update=True)
    if _is_cus_call_pending(doc):
        frappe.throw(_("Confirm the APP order before printing its KOT."))
    # Issue the BOM ingredients before acknowledging the KOT. If stock cannot
    # move (for example, a configured ingredient is short), the transaction
    # rolls back and the ticket remains unfired rather than silently drifting.
    from luuvcrm.api.inventory import consume_order_stock

    stock = consume_order_stock(order_name)
    doc = frappe.get_doc("POS Order", order_name, for_update=True)
    fired_new_lines = False
    for i in (doc.get("items") or []):
        if int(i.get("sent_qty") or 0) != int(i.qty or 0):
            fired_new_lines = True
            frappe.db.set_value("POS Order Item", i.name, "sent_qty", int(i.qty or 0))
    # This installation runs without a separate kitchen-status screen. Firing
    # new KOT lines is therefore the authoritative start of cooking. The
    # reprint path deliberately never calls this endpoint, and a redundant
    # acknowledgement with no new lines leaves the current status untouched.
    status = doc.kitchen_status
    if fired_new_lines and status not in ("Served", "Cancelled"):
        status = "Processing"
        frappe.db.set_value("POS Order", order_name, "kitchen_status", status)
    return {"ok": True, "stock": stock, "call_confirmed": False,
            "status": status}
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
    branch.require_access("POS Order", order_name)
    doc = frappe.get_doc("POS Order", order_name)
    fired = sum(int(i.get("sent_qty") or 0) for i in (doc.get("items") or []))
    if doc.pos_invoice or fired > 0:
        return {"ok": True, "deleted": False}
    total = float(doc.grand_total or 0)
    frappe.delete_doc("POS Order", order_name, ignore_permissions=True, force=True)
    # The row outlives the order it describes, which is the point: after the
    # delete there is nothing else anywhere that says this ticket existed.
    activity.log_activity(
        "order.delete", label="Deleted draft Rs %.0f" % total,
        ref_doctype="POS Order", ref_name=order_name, table=doc.restaurant_table,
        amount=total, total_before=total, total_after=0,
        detail={"items": len(doc.get("items") or [])})
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
    if staff:
        branch.require_access("POS Order", order_name)
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
    branch.require_access("Restaurant Table", table)
    orders = frappe.get_list("POS Order",
        filters={"docstatus": 0, "restaurant_table": table,
                 branch.FIELD: branch.viewing(), **list_filter()},
        fields=["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
                "grand_total", "creation", "order_source", "notes", "kitchen_status",
                "pos_invoice", "discount_amount", "discount_note"],
        order_by="creation asc"
    )
    # Exclude paid orders (have invoice)
    orders = [o for o in orders if not o.pos_invoice]
    lines = lines_by_order([o.name for o in orders])
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        o["items"] = [_line_dict(i) for i in lines.get(o.name, [])]
    _tag_registered_customers(orders)
    return {"orders": orders}
@frappe.whitelist()
def get_my_orders():
    """Today's orders for the active restaurant, shared by all floor staff.

    A table remains the same table when the waiter changes.  This powers the
    waiter app's ``Orders`` tab, where any waiter can add a later request to the
    existing unpaid order or mark it served.
    """
    roles.require_waiter()
    today = frappe.utils.today()
    orders = frappe.get_list("POS Order",
        filters={"creation": [">=", today],
                 branch.FIELD: branch.viewing()},
        fields=["name", "restaurant_table", "kitchen_status", "grand_total", "creation",
                "order_source", "waiter_name", "notes", "pos_invoice", "owner"],
        order_by="creation desc",
        limit_page_length=SCREEN_ORDER_CEILING,
    )
    lines = lines_by_order([o.name for o in orders])
    staff_logs = _waiter_order_staff_logs(orders)
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        o["paid"] = 1 if o.get("pos_invoice") else 0
        o["items"] = [_line_dict(i) for i in lines.get(o.name, [])]
        o["staff_log"] = staff_logs.get(o.name, [])
    return {"orders": orders}


def _waiter_order_staff_logs(orders):
    """Return the small, order-scoped audit trail shown to floor staff.

    The order document is the authority for who created the ticket.  Every
    later waiter append already writes one ``order.append`` LF Activity Log row
    with the authenticated session user, so this only reads and groups those
    rows; it does not invent a second audit mechanism.
    """
    logs = {}
    order_names = [o.name for o in orders]
    if not order_names:
        return logs

    # Creation is not a Version row in Frappe, so make it the first visible
    # event from the immutable document owner/creation fields.
    for o in orders:
        creator = o.get("waiter_name") or activity._full_name(o.get("owner")) or o.get("owner")
        logs[o.name] = [{
            "action": "created",
            "user": o.get("owner") or "",
            "user_full": creator or "",
            "created": str(o.get("creation") or ""),
            "time_ago": frappe.utils.pretty_date(o.get("creation")),
        }]

    # A partially migrated site must still be able to open the waiter screen.
    if not frappe.db.exists("DocType", activity.DOCTYPE):
        return logs

    appends = frappe.get_all(
        activity.DOCTYPE,
        filters={
            "ref_doctype": "POS Order",
            "ref_name": ["in", order_names],
            "action": "order.append",
            # Test databases can recycle a rolled-back naming-series value;
            # production restores can also leave an orphaned old activity row.
            # Neither belongs to this incarnation of the order.
            "creation": [">=", min(o.get("creation") for o in orders)],
        },
        fields=["ref_name", "creation", "user", "user_full", "qty"],
        order_by="creation asc",
        limit_page_length=1000,
    )
    for row in appends:
        logs.setdefault(row.ref_name, []).append({
            "action": "added",
            "user": row.user or "",
            "user_full": row.user_full or row.user or "",
            "created": str(row.creation or ""),
            "time_ago": frappe.utils.pretty_date(row.creation),
            "lines": int(row.qty or 0),
        })
    return logs
@frappe.whitelist(methods=["POST"])
def waiter_append_items():
    """Append NEW item lines to an existing unpaid draft order (waiter 'add to the bill').
    Append-only — existing/fired rows are untouched, so a KOT reprint (get_kot_print_data +
    mark_kot_printed, keyed on sent_qty) fires ONLY these new lines. This deliberately avoids
    update_order's full rebuild, which folds multiple lines of the same (item, takeaway) and
    would mis-carry sent_qty onto a freshly-added same-item line."""
    roles.require_waiter()
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not items:
        frappe.throw(_("At least one item is required"))

    branch.require_access("POS Order", order_name)
    return _append_waiter_items(order_name, items)


def _append_waiter_items(order_name, items):
    """Append priced lines to one branch-visible, unpaid order."""
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only draft orders can be added to"))
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot add items — payment already processed"))
    _require_cus_call_released(pos_order)

    before_total = float(pos_order.grand_total or 0)
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
    from luuvcrm.api.inventory import consume_order_stock

    consume_order_stock(pos_order.name)

    activity.log_activity(
        "order.append", label="Added %d line(s) to the bill" % len(items),
        ref_doctype="POS Order", ref_name=pos_order.name,
        table=pos_order.restaurant_table, qty=len(items),
        amount=float(pos_order.grand_total or 0) - before_total,
        total_before=before_total, total_after=float(pos_order.grand_total or 0),
        detail={"added": [it.get("item") for it in items]})
    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "appended": True,
    }
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
        if not frappe.db.exists("POS Order", order_name):
            result[key] = None
            continue
        branch.require_access("POS Order", order_name)
        doc = frappe.get_doc("POS Order", order_name)
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

    branch.require_access("POS Order", order_name)
    # Serialize item autosaves with the separate customer/phone save and payment.
    pos_order = frappe.get_doc("POS Order", order_name, for_update=True)
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
    before_total = float(pos_order.grand_total or 0)

    order_type = pos_order.order_type or ""
    # Preserve per-line "already fired to kitchen" qty across the full items rebuild,
    # so a re-print of the KOT shows only the newly added items.
    # Keyed by (item, takeaway) so a same-item dine-in/takeaway split keeps each line's fired qty.
    # Also preserve each line's per-item note (the POS doesn't send notes back, so without this a
    # cashier autosave would wipe the waiter's kitchen instructions). Distinct notes for the same
    # (item, takeaway) group are joined, since the POS folds them into a single cart line.
    _ensure_item_notes_field()
    sent_map = {}
    consumed_map = {}
    notes_map = {}
    for i in (pos_order.get("items") or []):
        key = (i.item, int(i.get("takeaway") or 0))
        sent_map[key] = sent_map.get(key, 0) + int(i.get("sent_qty") or 0)
        consumed_map[key] = consumed_map.get(key, 0) + float(i.get("stock_consumed_qty") or 0)
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
        row = {"item": item_code, "item_name": item_name, "qty": qty, "rate": rate,
               "takeaway": ta_flag,
               "sent_qty": min(sent_map.get((item_code, ta_flag), 0), qty),
               "notes": line_note}
        if frappe.db.has_column("POS Order Item", "stock_consumed_qty"):
            # May intentionally exceed the new qty after an after-KOT
            # reduction: cooked food is not put back into raw-material stock.
            row["stock_consumed_qty"] = consumed_map.get((item_code, ta_flag), 0)
        pos_order.append("items", row)
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    # What the kitchen had already fired and this rebuild took back off the
    # ticket. sent_map is the fired qty per line before the rebuild; each new
    # line keeps min(fired, qty), so the difference is food that was cooked and
    # is no longer on the bill. This is the whole point of the after-kot flag.
    fired_pulled = max(sum(sent_map.values())
                       - sum(int(i.get("sent_qty") or 0) for i in pos_order.get("items")), 0)

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

    after_total = float(pos_order.grand_total or 0)
    # Two rules can fire on one edit; the more serious one wins the row, and the
    # note carries both so the manager reads why.
    flag, note = activity.late_edit_flag(was_printed, "edited")
    kot_flag, kot_note = activity.kot_pull_flag(fired_pulled, "fired items")
    if kot_flag and not flag:
        flag, note = kot_flag, kot_note
    elif kot_flag:
        note = "%s; %s" % (note, kot_note)
    activity.log_activity(
        "order.update",
        label="Bill edited Rs %.0f → Rs %.0f" % (before_total, after_total),
        ref_doctype="POS Order", ref_name=pos_order.name,
        bill_no=pos_order.pos_invoice or None, table=pos_order.restaurant_table,
        amount=after_total - before_total,
        total_before=before_total, total_after=after_total,
        flag_reason=flag, flag_note=note,
        detail={"items": len(items), "fired_pulled": fired_pulled,
                "was_printed": was_printed})

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
    branch.require_access("POS Order", order_name)

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
    roles.require_waiter()
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    table = (data.get("table") or "").strip()
    if not order_name:
        frappe.throw(_("Order name is required"))

    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Only open orders can be moved"))
    if pos_order.pos_invoice:
        frappe.throw(_("Cannot move a paid order"))
    if table and not frappe.db.exists("Restaurant Table", table):
        frappe.throw(_("Table {0} not found").format(table))
    if table:
        target_restaurant = branch.require_access("Restaurant Table", table)
        if (pos_order.get(branch.FIELD) and target_restaurant
                and pos_order.get(branch.FIELD) != target_restaurant):
            frappe.throw(_("An order cannot be moved to another restaurant"),
                         frappe.PermissionError)

    old_table = pos_order.restaurant_table
    pos_order.db_set("restaurant_table", table)

    activity.log_activity(
        "order.move", label="Moved %s → %s" % (old_table or "no table", table or "no table"),
        ref_doctype="POS Order", ref_name=order_name, table=table or old_table,
        amount=float(pos_order.grand_total or 0),
        detail={"from": old_table or None, "to": table or None})
    return {"ok": True, "name": order_name, "old_table": old_table or "", "table": table}
