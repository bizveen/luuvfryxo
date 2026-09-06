"""Manager sign-off: what needs it, who may give it, and what it does when given.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import roles

from luuvcrm.api.expenses import _decide_expense
from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.purchases import _decide_purchase_bill
from luuvcrm.api.schema import (
    _clean_item_note,
    _ensure_item_notes_field,
    _install_pos_approval,
)


@frappe.whitelist(methods=["POST"])
def create_approval():
    """POS raises an approval request for a bill change that needs a manager."""
    roles.require_pos()
    if not frappe.db.exists("DocType", "POS Approval"):
        _install_pos_approval()
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
    # Pending has no date bound: a request nobody answers is read again on every
    # poll, for ever. 200 is far past a real queue — reaching it means approvals
    # are not being answered, which is the manager's problem, not a page's.
    pending = frappe.get_list("POS Approval", filters=pending_filters,
                             fields=fields, order_by="requested_at desc",
                             limit_page_length=200)
    if not include_recent:
        return pending
    recent = frappe.get_list("POS Approval", filters=recent_filters,
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
            # roles.block_self_approval() is what actually enforces it.
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
    while their own stays blocked by roles.block_self_approval().

    Pending only. This tab is a queue to empty, not a history — a resolved bill
    belongs in the Bills list, where it already shows with its status."""
    roles.require_pos()
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
    roles.require_pos()
    data = frappe.local.form_dict
    name = data.get("name")
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        frappe.throw(_("Decision must be 'approved' or 'rejected'"))
    doc = frappe.get_doc("POS Approval", name)
    if doc.status != "pending":
        frappe.throw(_("Already resolved"))
    roles.block_self_approval(doc.requested_by)
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
        rate = _server_item_rate(item_code)
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
def _install_check_printed():
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
@frappe.whitelist()
def setup_check_printed():
    """Schema setup over HTTP. Admin only; the work is in _install_check_printed."""
    frappe.only_for("System Manager")
    return _install_check_printed()
@frappe.whitelist(methods=["POST"])
def mark_check_printed():
    """Flag that a guest check was printed for an order."""
    roles.require_pos()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    if not frappe.db.has_column("POS Order", "check_printed"):
        _install_check_printed()
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
    rows = frappe.get_list("POS Approval", filters={"status": "pending"},
                          fields=["name", "requested_at", "action", "action_payload"],
                          limit_page_length=200)
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
