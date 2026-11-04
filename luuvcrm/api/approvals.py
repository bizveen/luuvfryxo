"""Manager sign-off: what needs it, who may give it, and what it does when given.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import activity, branch, roles

from luuvcrm.api.expenses import _decide_expense
from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.purchases import (
    _apply_bill_discount,
    _apply_purchase_line_categories,
    _decide_purchase_bill,
)
from luuvcrm.api.expenses import category_is_stock
from luuvcrm.api.schema import (
    _clean_item_note,
    _ensure_item_notes_field,
    _install_pos_approval,
)


def _discount_order_snapshot(details_json, pos_order):
    """Keep the order behind a discount request readable after it changes.

    A manager is deciding whether *this particular meal* deserves a discount,
    so the card needs the items and current total, not only "10%".  Store a
    snapshot on the request: reading the live order later would make the card's
    evidence change while it waits, or disappear after the order is paid.
    """
    try:
        details = json.loads(details_json or "{}")
    except (TypeError, ValueError):
        details = {}
    if details.get("order_lines") or not pos_order or not frappe.db.exists("POS Order", pos_order):
        return details
    order = frappe.get_doc("POS Order", pos_order)
    details["order_lines"] = [{
        "qtyStr": "%s×" % (int(row.qty) if float(row.qty or 0).is_integer() else row.qty),
        "name": row.item_name or row.item,
        "totalStr": "Rs %s" % f"{float(row.qty or 0) * float(row.rate or 0):,.0f}",
    } for row in (order.get("items") or [])]
    details["order_total"] = "Rs %s" % f"{float(order.grand_total or 0):,.0f}"
    details["order_context"] = " · ".join(filter(None, [
        order.order_type or "",
        ("Waiter " + order.waiter_name) if order.waiter_name else "",
        ("Customer " + order.customer_name) if order.customer_name else "",
    ]))
    return details


@frappe.whitelist(methods=["POST"])
def create_approval():
    """POS raises an approval request for a bill change that needs a manager."""
    roles.require_pos()
    if not frappe.db.exists("DocType", "POS Approval"):
        _install_pos_approval()
    data = frappe.local.form_dict
    approval_type = data.get("approval_type", "void")
    pos_order = data.get("pos_order", "")
    if pos_order:
        branch.require_access("POS Order", pos_order)
    action = data.get("action", "")
    action_payload = data.get("action_payload", "") or ""
    details_json = data.get("details_json", "") or ""
    if approval_type == "discount":
        details_json = json.dumps(_discount_order_snapshot(details_json, pos_order))

    # Dedupe. The POS leaves a removed or edited item on screen until the manager
    # approves, so nothing visibly changes after a tap and the cashier taps again
    # — each tap used to raise a fresh approval, and one order once carried ten
    # identical "remove" cards. An identical request that is still pending is
    # returned instead of a second one being made. The key is the *whole* action:
    # same order, same type, same action, same payload. A different item or qty
    # produces a different payload and still raises its own approval, so only
    # true repeats collapse.
    existing = frappe.db.get_value("POS Approval", {
        "status": "pending",
        "approval_type": approval_type,
        "pos_order": pos_order,
        "action": action,
        "action_payload": action_payload,
        # `requested_by` is in the key, which `main`'s version does not have.
        # Without it, a second cashier asking for the identical thing is handed
        # the first one's card — and can then resolve it, because the record
        # says somebody else raised it. That is the exact hole
        # roles.block_self_approval() exists to close, and the self-approval
        # test caught it the moment the dedupe went in. Two people asking is two
        # requests; one person tapping twice is one.
        "requested_by": frappe.session.user,
    }, "name")
    if existing:
        # Deliberately no activity row: this is the same request, not a second
        # one, and the row for it was written when it was first raised.
        return {"ok": True, "name": existing, "duplicate": True}

    doc = frappe.get_doc({
        "doctype": "POS Approval",
        "approval_type": approval_type,
        "pos_order": pos_order,
        "title": data.get("title", "") or "Approval request",
        "subhead": data.get("subhead", ""),
        "details_json": details_json,
        "flag_text": data.get("flag_text", ""),
        "action": action,
        "action_payload": action_payload,
        "status": "pending",
        "requested_by": frappe.session.user,
        "requested_at": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    _supersede_older_edits(doc)

    # The action name matters: activity.duplicate_approval_flag() counts earlier
    # rows with exactly this action and this order to spot the cashier asking
    # twice, or asking a second manager.
    order = pos_order
    flag, note = activity.duplicate_approval_flag(order, approval_type)
    # One row, hung off the *order* rather than the approval, so a bill's story
    # reads top to bottom — and so the duplicate counter, which looks up rows by
    # order name, finds exactly one per request.
    activity.log_activity(
        "approval.create", label="Asked for %s approval" % approval_type,
        ref_doctype="POS Order" if order else "POS Approval",
        ref_name=order or doc.name, item=approval_type,
        detail={"approval": doc.name, "title": doc.title, "action": doc.action or None},
        flag_reason=flag, flag_note=note)
    return {"ok": True, "name": doc.name}
# Statuses a card can end in besides the manager's two decisions. "superseded"
# is written with db_set (no validation round-trip), so it works even before
# the DocType JSON carrying the option has been migrated in.
SUPERSEDED = "superseded"


def _supersede_older_edits(doc):
    """A newer "remove item" card for the same bill replaces every older one
    still pending. Each card carries the *whole* item list the bill should end
    up with, so two of them are not two changes - they are two snapshots, and
    only the latest describes what the cashier now wants. Live (POS-03084,
    2026-09-10): the newer card was approved first, then the older one, which
    put the older list back and wiped an item added in between."""
    if not doc.pos_order or (doc.action or "") != "update_items":
        return []
    older = frappe.get_all("POS Approval", filters={
        "status": "pending", "action": "update_items", "pos_order": doc.pos_order,
        "name": ["!=", doc.name],
        "requested_at": ["<=", doc.requested_at],
    }, pluck="name")
    for name in older:
        frappe.db.set_value("POS Approval", name, {
            "status": SUPERSEDED,
            "resolved_by": frappe.session.user,
            "resolved_at": now_datetime(),
        }, update_modified=True)
    return older


def _is_stale_edit(doc):
    """True when a newer "remove item" card exists for the same bill - this one
    describes an item list the cashier has since moved past."""
    if not doc.pos_order or (doc.action or "") != "update_items":
        return False
    return bool(frappe.db.exists("POS Approval", {
        "action": "update_items", "pos_order": doc.pos_order,
        "name": ["!=", doc.name],
        "requested_at": [">", doc.requested_at],
    }))


def _void_order(order_name):
    """Cancel a draft, unpaid POS Order. Returns True when it did; False when the
    bill is not there to void (already cancelled, or already paid)."""
    po = frappe.get_doc("POS Order", order_name)
    if po.docstatus != 0 or po.pos_invoice:
        return False
    old_status = po.kitchen_status
    po.db_set("docstatus", 2)
    po.db_set("kitchen_status", "Cancelled")
    frappe.get_doc({
        "doctype": "Version", "ref_doctype": "POS Order", "docname": order_name,
        "data": frappe.as_json({"changed": [
            ["docstatus", "0", "2"],
            ["kitchen_status", old_status or "Pending", "Cancelled"]]}),
        "owner": frappe.session.user, "modified_by": frappe.session.user,
        "creation": now_datetime(),
    }).insert(ignore_permissions=True)
    return True


def _order_is_moot(order_name):
    """Why a bill change can no longer be applied, or None when it still can.
    Approving a card whose bill has gone must not read as "done" - for weeks
    the manager saw "Approved" while the till kept the item, because the
    execution failed into Error Log and nobody was told (89 times, 27 Aug -
    9 Sep 2026)."""
    if not frappe.db.exists("POS Order", order_name):
        return _("Bill no longer exists")
    docstatus, invoice = frappe.db.get_value("POS Order", order_name, ["docstatus", "pos_invoice"])
    if docstatus == 2:
        return _("Bill was already voided")
    if docstatus == 1 or invoice:
        return _("Bill was already paid")
    return None


def _run_bill_action(doc):
    """Apply an approved bill change. Returns (executed, note): `executed` names
    what happened for the trail; `note` is a sentence for the manager when the
    card could not be applied as asked (it is then closed as rejected)."""
    action = doc.action or ""
    reason = _order_is_moot(doc.pos_order)
    if reason:
        return None, reason
    if action == "void":
        return ("voided" if _void_order(doc.pos_order) else None), None
    if action == "update_items":
        if _is_stale_edit(doc):
            return None, _("A newer change to this bill replaced this one")
        payload = json.loads(doc.action_payload or "{}")
        items = payload.get("items") or []
        if not items:
            # Every item taken off the bill. An order cannot be saved with no
            # items (41 of the 89 silent failures), and a bill with nothing on
            # it is a void - so that is what this is.
            return ("voided" if _void_order(doc.pos_order) else None), None
        _apply_order_items(doc.pos_order, items)
        return "items_updated", None
    if action == "discount":
        payload = json.loads(doc.action_payload or "{}")
        _apply_order_discount(doc.pos_order, payload)
        return "discounted", None
    return None, None


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
    active = branch.viewing()
    pending_filters = {"status": "pending", branch.FIELD: active}
    recent_filters = [["status", "in", ["approved", "rejected"]],
                      ["modified", ">=", frappe.utils.today()],
                      [branch.FIELD, "=", active]]
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
    # Whether *this* viewer may answer their own card depends only on their
    # roles, so ask once rather than per row — same reason as `names` above.
    self_ok_types = roles.SELF_APPROVABLE_TYPES if roles.is_manager() else frozenset()
    category_options = []
    line_category_options = []
    if any((r.get("action") or "") in ("purchase_bill", "expense") for r in rows):
        category_options = [{"value": "", "label": "Purchase Bill (stock items)"}]
        if frappe.db.exists("DocType", "Expense Category"):
            category_rows = frappe.get_all(
                "Expense Category", filters={"disabled": 0}, fields=["name"], order_by="name",
            )
            category_options += [{"value": r.name, "label": r.name} for r in category_rows]
            line_category_options = [{"value": r.name, "label": r.name,
                                      "in_stock": 1 if category_is_stock(r.name) else 0}
                                     for r in category_rows]

    # Purchase-bill cards show each line's price with a "was Rs X" delta when the
    # buying price moved — the one number that says whether a supplier put their
    # prices up. The stored card lines carry only qty/name/total, so price,
    # old_price, qty and unit are overlaid from the linked bill: one query for
    # every bill on screen at once, matched to the card lines by row order,
    # because both are built in the same loop order in create_purchase_bill.
    bill_of = {}
    for r in rows:
        if (r.get("action") or "") == "purchase_bill":
            try:
                bill = json.loads(r.get("action_payload") or "{}").get("bill")
            except Exception:
                bill = None
            if bill:
                bill_of[r.get("name")] = bill
    bill_items = {}
    if bill_of:
        item_fields = ["name", "parent", "idx", "item", "qty", "price", "old_price", "uom"]
        if frappe.db.has_column("Purchase Bill Item", "category"):
            item_fields.append("category")
        for it in frappe.get_all("Purchase Bill Item",
                                 filters={"parent": ["in", list(set(bill_of.values()))]},
                                 fields=item_fields,
                                 order_by="parent asc, idx asc", limit_page_length=0):
            bill_items.setdefault(it.parent, []).append(it)

    for r in rows:
        try:
            details = json.loads(r.get("details_json") or "{}")
        except Exception:
            details = {}
        if (r.get("approval_type") or "") == "discount" and not details.get("order_lines"):
            # Enrich requests raised just before this feature was deployed while
            # their linked order still exists. Truly old/orphaned cards remain
            # explicit about missing evidence instead of inventing it.
            details = _discount_order_snapshot(r.get("details_json"), r.get("pos_order"))
        # Purchase-bill / expense approvals carry a receipt photo on the linked
        # doc — surface it so the approver can inspect the actual bill.
        image = ""
        expense_category = ""
        try:
            payload = json.loads(r.get("action_payload") or "{}")
            action = r.get("action") or ""
            if action == "purchase_bill" and payload.get("bill"):
                image = frappe.db.get_value("Purchase Bill", payload["bill"], "receipt_image") or ""
            elif action == "expense" and payload.get("expense"):
                expense = frappe.db.get_value(
                    "Expense", payload["expense"], ["receipt_image", "category"], as_dict=True,
                )
                if expense:
                    image = expense.receipt_image or ""
                    expense_category = expense.category or ""
        except Exception:
            image = ""

        # Overlay the bill's per-line price detail onto this card's lines.
        lines = details.get("lines") or []
        bill = bill_of.get(r.get("name"))
        if bill and bill_items.get(bill):
            items = bill_items[bill]
            for i, line in enumerate(lines):
                if i < len(items) and isinstance(line, dict):
                    it = items[i]
                    line["qty"] = it.qty
                    line["price"] = it.price
                    line["old_price"] = it.old_price
                    line["unit"] = it.uom
                    line["row_name"] = it.name
                    line["has_item"] = 1 if it.item else 0
                    if it.get("category"):
                        line["category"] = it.category
                    line["qtyStr"] = (f"{float(it.qty or 0):g} {it.uom or 'Nos'}"
                                      if category_is_stock(it.get("category")) else "")

        bill_categories = {line.get("category") for line in lines if line.get("category")}

        out.append({
            "name": r.get("name"),
            "type": r.get("approval_type") or "item",
            "pos_order": r.get("pos_order") or "",
            "title": r.get("title") or "",
            "subhead": r.get("subhead") or "",
            "lines": lines,
            "footer_label": details.get("footer_label") or "",
            "footer_value": details.get("footer_value") or "",
            "order_lines": details.get("order_lines") or [],
            "order_total": details.get("order_total") or "",
            "order_context": details.get("order_context") or "",
            "order_context_missing": ((r.get("approval_type") or "") == "discount"
                                      and not details.get("order_lines")),
            "flag_text": r.get("flag_text") or "",
            "status": r.get("status") or "pending",
            "time": frappe.utils.pretty_date(r.get("requested_at")),
            "image": image,
            "expense_category": expense_category,
            # The exact same classifier as New bill / expense: blank means a
            # stock Purchase Bill; every other value is an Expense Category.
            "category_value": expense_category if (r.get("action") or "") == "expense" else "",
            "category_options": (category_options
                                 if (r.get("action") or "") in ("purchase_bill", "expense") else []),
            "line_category_options": (line_category_options
                                      if (r.get("action") or "") == "purchase_bill" else []),
            "multi_category": ((r.get("action") or "") == "purchase_bill"
                               and len(bill_categories) > 1),
            "manager_comment": r.get("manager_comment") or "",
            "by": names.get(r.get("requested_by"), ""),
            # Whoever raised it cannot resolve it — the UI locks the card,
            # roles.block_self_approval() is what actually enforces it. Not a
            # flat rule any more: a manager answers their own *edit* (the
            # manager app is manager-gated, so an edit card there is never
            # locked), while every other type stays two-handed.
            "mine": bool(r.get("requested_by")) and r.get("requested_by") == me,
            "self_ok": (r.get("approval_type") or "") in self_ok_types,
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
    app shows, with every other approval type left out. Open to whoever holds a
    purchasing role, not managers only: one Purchaser can clear a colleague's bill,
    while their own stays blocked by roles.block_self_approval(). It used to be
    open to any POS user, which put the price book in the hands of the whole till.

    Pending only. This tab is a queue to empty, not a history — a resolved bill
    belongs in the Bills list, where it already shows with its status."""
    roles.require_purchases()
    rows = _pending_and_recent_approvals("purchase_bill", include_recent=False)
    return {"approvals": _format_approvals(rows), "pending": len(rows)}
@frappe.whitelist()
def get_approval_status(name):
    if (not name or not frappe.db.exists("DocType", "POS Approval")
            or not frappe.db.exists("POS Approval", name)):
        return {"status": "unknown"}
    branch.require_access("POS Approval", name)
    return {"status": frappe.db.get_value("POS Approval", name, "status") or "unknown"}


def _purchase_bill_to_expense(bill_name, category, approval, discount=None):
    """Reclassify a pending stock bill as an approved expense without losing it.

    The original Purchase Bill stays as a rejected audit record (and therefore
    cannot receive stock or rewrite the buying price book).  The approved amount
    lives on a new Expense carrying the same payee, date, note, receipt, branch,
    requester and request time.  Both changes share this request's transaction.

    The reverse conversion is deliberately not guessed: an Expense has only one
    amount and no item/qty/unit rows, so it cannot become a valid stock receipt.
    """
    if not frappe.db.exists("Expense Category", {"name": category, "disabled": 0}):
        frappe.throw(_("Pick an active expense category"))
    bill = frappe.get_doc("Purchase Bill", bill_name)
    if bill.status != "pending":
        frappe.throw(_("This purchase bill has already been resolved"))
    if discount not in (None, ""):
        _apply_bill_discount(bill, discount)
        bill.reload()

    values = {
        "doctype": "Expense",
        "category": category,
        "payee": bill.vendor or "",
        "amount": float(bill.total or 0),
        "expense_date": bill.purchase_date,
        "note": bill.note or "",
        "receipt_image": bill.receipt_image or "",
        "status": "pending",
        "requested_by": bill.requested_by,
        "requested_at": bill.requested_at,
    }
    if bill.meta.has_field(branch.FIELD) and frappe.get_meta("Expense").has_field(branch.FIELD):
        values[branch.FIELD] = bill.get(branch.FIELD)
    expense = frappe.get_doc(values)
    expense.flags.ignore_permissions = True
    expense.insert(ignore_permissions=True)

    _decide_purchase_bill(bill.name, "rejected")
    _decide_expense(expense.name, "approved")
    approval.db_set("action", "expense")
    approval.db_set("action_payload", json.dumps({
        "expense": expense.name,
        "purchase_bill": bill.name,
        "discount": float(bill.discount or 0),
    }))
    approval.db_set("title", category)
    approval.db_set("subhead", ((bill.vendor or "") + " · " if bill.vendor else "") + "expense")
    activity.log_activity(
        "purchase_bill.reclassified",
        label="Purchase bill reclassified as %s" % category,
        ref_doctype="Expense", ref_name=expense.name, amount=expense.amount,
        detail={"purchase_bill": bill.name, "approval": approval.name, "category": category,
                "discount": float(bill.discount or 0)},
    )
    return expense.name


@frappe.whitelist(methods=["POST"])
def resolve_approval():
    """Approve or reject a POS Approval; on approve the action is executed.

    Who may answer depends on what is being answered, so the gate is in two
    steps. First, anybody who could answer *something* — the till or purchasing
    — so a guest or the kitchen is turned away before a row is even loaded. Then
    the approval's own kind decides: a purchase bill needs a purchasing role, and
    everything else (a discount, a void, an edited order, an expense) a POS role.

    A single gate cannot be right. `require_pos()` alone let any cashier decide
    purchase bills — and rewrite the buying price book — while refusing a
    Purchaser, who is not a POS role, on the one screen built for them: the
    /purchases queue approves through this endpoint, not resolve_purchase_bill.
    """
    if not roles.has_any(roles.POS_ROLES | roles.PURCHASE_ROLES):
        frappe.throw(_("You are not permitted to answer approvals"), frappe.PermissionError)
    data = frappe.local.form_dict
    name = data.get("name")
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        frappe.throw(_("Decision must be 'approved' or 'rejected'"))
    branch.require_access("POS Approval", name)
    doc = frappe.get_doc("POS Approval", name)
    if (doc.action or "") == "purchase_bill":
        roles.require_purchases()
    else:
        roles.require_pos()
    if doc.status != "pending":
        frappe.throw(_("Already resolved"))
    # The type goes in because one of them — a bill edit — a manager may
    # answer for themselves. roles.SELF_APPROVABLE_TYPES says which.
    roles.block_self_approval(doc.requested_by, doc.approval_type)
    doc.db_set("status", decision)
    doc.db_set("resolved_by", frappe.session.user)
    doc.db_set("resolved_at", now_datetime())
    comment = (data.get("comment") or "").strip()
    if comment and frappe.db.has_column("POS Approval", "manager_comment"):
        doc.db_set("manager_comment", comment[:500])

    executed = None
    note = None
    action = (doc.action or "")
    if action == "purchase_bill":
        payload = json.loads(doc.action_payload or "{}")
        bill = payload.get("bill")
        if bill and frappe.db.exists("Purchase Bill", bill):
            # A purchase approval may carry a discount - what the vendor took
            # off the whole bill. The manager is looking at the paper when
            # they approve, which is the only moment anybody can read that
            # figure off it.
            category = (data.get("category") or "").strip()
            line_categories = data.get("line_categories")
            if decision == "approved" and category and line_categories:
                frappe.throw(_("Choose either one bill category or per-line categories, not both"))
            if decision == "approved" and line_categories:
                purchase_doc = frappe.get_doc("Purchase Bill", bill)
                _apply_purchase_line_categories(purchase_doc, line_categories)
            if decision == "approved" and category:
                _purchase_bill_to_expense(
                    bill, category, doc, discount=data.get("discount"),
                )
                executed = "purchase_reclassified_as_expense"
            else:
                _decide_purchase_bill(bill, decision, discount=data.get("discount"))
                executed = "purchase_" + decision
    elif action == "expense":
        # Runs on reject too - that's what pulls the drawer pay-out back out.
        payload = json.loads(doc.action_payload or "{}")
        expense = payload.get("expense")
        if expense and frappe.db.exists("Expense", expense):
            # The approver is the second pair of eyes. Let them correct a wrongly
            # classified expense as part of approval instead of approving bad
            # reporting first and hunting for the edit screen afterwards.
            category_sent = "category" in data
            category = (data.get("category") or "").strip()
            if decision == "approved" and category_sent and not category:
                frappe.throw(_(
                    "A stock Purchase Bill needs item, quantity and unit-price lines. "
                    "Reject this expense and enter it again as a Purchase Bill."
                ))
            if decision == "approved" and category:
                if not frappe.db.exists("Expense Category", {"name": category, "disabled": 0}):
                    frappe.throw(_("Pick an active expense category"))
                expense_doc = frappe.get_doc("Expense", expense)
                old_category = expense_doc.category or ""
                if category != old_category:
                    expense_doc.db_set("category", category)
                    doc.db_set("title", category)
                    doc.db_set(
                        "subhead",
                        ((expense_doc.payee or "") + " · " if expense_doc.payee else "") + "expense",
                    )
                    activity.log_activity(
                        "expense.category_at_approval",
                        label="Expense category corrected: %s → %s" % (old_category, category),
                        ref_doctype="Expense", ref_name=expense, amount=expense_doc.amount,
                        detail={"from": old_category, "to": category, "approval": doc.name},
                    )
            _decide_expense(expense, decision)
            executed = "expense_" + decision
    elif decision == "approved" and doc.pos_order:
        # No try/except here any more. A failure used to be logged and the card
        # still marked approved - the manager saw a green tick, the till saw
        # "approved", and the bill was unchanged. Now an unexpected error rolls
        # the decision back (the card stays pending) and reaches the manager.
        executed, note = _run_bill_action(doc)
        if note:
            # Nothing to apply: the card is closed as rejected with the reason,
            # so the queue clears and the manager reads why.
            decision = "rejected"
            doc.db_set("status", decision)
            if frappe.db.has_column("POS Approval", "manager_comment"):
                doc.db_set("manager_comment", (comment + " / " if comment else "") + note)

    discount = data.get("discount")
    activity.log_activity(
        "approval.resolve",
        label="%s %s%s" % (decision.capitalize(), doc.approval_type or "request",
                           (" · discount Rs %s" % discount) if discount else ""),
        ref_doctype="POS Order" if doc.pos_order else "POS Approval",
        ref_name=doc.pos_order or doc.name, item=doc.approval_type or None,
        amount=float(discount) if discount else None,
        detail={"approval": doc.name, "decision": decision, "executed": executed,
                "requested_by": doc.requested_by, "comment": comment or None,
                "note": note, "discount": discount or None})
    return {"ok": True, "status": decision, "executed": executed, "note": note}
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
    branch.require_access("POS Order", order_name)
    if not frappe.db.has_column("POS Order", "check_printed"):
        _install_check_printed()
    frappe.db.set_value("POS Order", order_name, "check_printed", 1)
    return {"ok": True}
@frappe.whitelist()
def get_order_flags(order_name):
    """Lightweight flags the POS needs to decide if an edit requires approval."""
    roles.require_pos()
    if not order_name:
        return {}
    branch.require_access("POS Order", order_name)
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
    rows = frappe.get_list("POS Approval", filters={"status": "pending",
                                                     branch.FIELD: branch.viewing()},
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
    condition, params = branch.sql_restaurant()
    row = frappe.db.sql(
        """select count(case when status='pending' then 1 end), max(modified)
           from `tabPOS Approval` where 1=1""" + condition, params)[0]
    pending = int(row[0] or 0)
    return {"pending": pending, "sig": f"{row[1]}|{pending}"}
