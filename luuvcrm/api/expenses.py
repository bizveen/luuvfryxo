"""Utility bills and other outgoings.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import branch, roles

from luuvcrm.api.schema import _install_pos_approval
from luuvcrm.api.shifts import _open_shift_name, _resolve_pos_profile_name


# ─── Expenses (utility bills & other outgoings) ────────────────────
# A CEB bill, gas, rent. Deliberately NOT a Purchase Bill — no items, no qty × price, nothing to
# feed the price book. Like the rest of this app it is not GL-integrated, and by design it does not
# touch the cash drawer either (see create_expense) — it is a record with a manager sign-off.

FOOD_PURCHASE_CATEGORY = "Food / Ingredients"
STAFF_MEALS_CATEGORY = "Staff Meals"
# Only the purchase/ingredients bucket creates reusable Item records and stock
# movements. Every other category is an expense line, even when it shares one
# supplier receipt with stock purchases.
STOCK_EXPENSE_CATEGORIES = frozenset({FOOD_PURCHASE_CATEGORY})
# Categories with a reusable item/price list in the purchase bill form.  This
# is deliberately separate from stock: a staff meal should be quick to pick
# again, but it must not receive ingredients into the warehouse.
ITEM_LIST_EXPENSE_CATEGORIES = frozenset({FOOD_PURCHASE_CATEGORY, STAFF_MEALS_CATEGORY})

# Purchases that belong in ERPNext's fixed-asset register instead of the
# restaurant's operating P&L.  They still use the same supplier-bill UI: the
# category is what changes the accounting treatment at approval.
FIXED_ASSET_PURCHASE_CATEGORIES = {
    "Kitchen Equipment": "Kitchen Equipment",
    "Electrical Equipment": "Electrical Equipment",
    "Furniture & Fixtures": "Furniture & Fixtures",
    "Signage": "Signage",
}

EXPENSE_CATEGORIES = [
    # Default for itemised supplier bills. Keeping it in the shared category
    # master lets one Food City receipt split cleanly between ingredients,
    # staff meals and office supplies without inventing a second taxonomy.
    FOOD_PURCHASE_CATEGORY,
    "Electricity", "Water", "Gas (LP)", "Rent", "Internet/Phone", "Salaries/Wages",
    "Repairs & Maintenance", "Transport/Fuel", "Cleaning & Consumables", "Licences/Fees", "Other",
    # The lines of the printed profit-and-loss statement that had no category.
    # A live site gets these from patches.v1_0.add_pnl_expense_categories; a
    # fresh one from this list (its patches are marked done without running).
    "Advertising", "Office Supplies", "Employee Houses Rent", "Staff Meals", "Staff Medical",
    "Municipal Council", "Taxes", "Charities",
    # Takeaway boxes, bags, cups bought as a running cost rather than on a stock
    # bill. Its own line on the statement; a live site gets it from
    # patches.v1_0.add_packaging_expense_category.
    "Packaging",
]
# The eight P&L categories above, for their patch: a site that already has
# categories is topped up with these and nothing else. Keep the names explicit;
# inserting a new default earlier in EXPENSE_CATEGORIES must not shift this set.
PNL_EXPENSE_CATEGORIES = [
    "Advertising", "Office Supplies", "Employee Houses Rent", "Staff Meals",
    "Staff Medical", "Municipal Council", "Taxes", "Charities",
]
PACKAGING_CATEGORY = "Packaging"
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
def _install_expenses():
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
                {"fieldname": "in_stock", "label": "In Stock", "fieldtype": "Check",
                 "default": "0", "in_list_view": 1,
                 "description": "On = supplier-bill lines become Items and stock."},
                {"fieldname": "has_item_list", "label": "Has Item List", "fieldtype": "Check",
                 "default": "0", "in_list_view": 1,
                 "description": "On = Add Bill shows the reusable item dropdown for this category."},
                {"fieldname": "is_fixed_asset", "label": "Fixed Asset", "fieldtype": "Check",
                 "default": "0", "in_list_view": 1,
                 "description": "On = approval creates a draft ERPNext Asset, not an expense."},
                {"fieldname": "asset_category", "label": "ERPNext Asset Category",
                 "fieldtype": "Link", "options": "Asset Category",
                 "depends_on": "is_fixed_asset", "mandatory_depends_on": "is_fixed_asset"},
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
            frappe.get_doc({"doctype": "Expense Category", "category_name": c,
                            "in_stock": int(c in STOCK_EXPENSE_CATEGORIES),
                            "has_item_list": int(c in ITEM_LIST_EXPENSE_CATEGORIES)}).insert(
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
        _install_pos_approval()
    _ensure_select_option("POS Approval", "approval_type", "expense")

    # Asset Categories must exist before the Link-valued purchase categories.
    # Keep this local to avoid an import cycle at module load time.
    from luuvcrm.api.assets import ensure_asset_purchase_categories
    ensure_asset_purchase_categories()

    frappe.clear_cache()
    return {"created": created}
@frappe.whitelist()
def setup_expenses():
    """Schema setup over HTTP. Admin only; the work is in _install_expenses."""
    frappe.only_for("System Manager")
    return _install_expenses()
@frappe.whitelist()
def get_expense_categories():
    """Categories for the /purchases expense form. Manager-editable from Desk > Expense Category."""
    roles.require_expenses()
    if not frappe.db.exists("DocType", "Expense Category"):
        _install_expenses()
    fields = ["name"]
    has_stock_flag = frappe.db.has_column("Expense Category", "in_stock")
    has_list_flag = frappe.db.has_column("Expense Category", "has_item_list")
    has_asset_flag = frappe.db.has_column("Expense Category", "is_fixed_asset")
    has_asset_category = frappe.db.has_column("Expense Category", "asset_category")
    if has_stock_flag:
        fields.append("in_stock")
    if has_list_flag:
        fields.append("has_item_list")
    if has_asset_flag:
        fields.append("is_fixed_asset")
    if has_asset_category:
        fields.append("asset_category")
    rows = frappe.get_all("Expense Category", filters={"disabled": 0},
                          fields=fields, order_by="name")
    details = [{"name": r.name,
                "in_stock": int(r.in_stock if has_stock_flag else
                                r.name in STOCK_EXPENSE_CATEGORIES),
                "has_item_list": int(r.has_item_list if has_list_flag else
                                     r.name in ITEM_LIST_EXPENSE_CATEGORIES),
                "is_fixed_asset": int(r.is_fixed_asset if has_asset_flag else
                                      r.name in FIXED_ASSET_PURCHASE_CATEGORIES),
                "asset_category": (r.asset_category if has_asset_category else
                                   FIXED_ASSET_PURCHASE_CATEGORIES.get(r.name, "")) or ""}
               for r in rows]
    return {"categories": [r["name"] for r in details],
            "expense_categories": [r["name"] for r in details if not r["is_fixed_asset"]],
            "category_details": details}


def category_is_stock(category):
    """Whether a supplier-bill line belongs in Items/BOM/stock.

    The fallback keeps Food / Ingredients working during the short deploy
    window before migrate adds the flag. Blank legacy bill lines are purchases.
    """
    category = category or FOOD_PURCHASE_CATEGORY
    if not frappe.db.has_column("Expense Category", "in_stock"):
        return category in STOCK_EXPENSE_CATEGORIES
    return bool(frappe.db.get_value("Expense Category", category, "in_stock"))


def category_has_item_list(category):
    """Whether Add Bill uses reusable Items instead of free-text lines.

    The named fallback keeps deploys usable between the Python release and the
    schema migration that adds ``has_item_list``.
    """
    category = category or FOOD_PURCHASE_CATEGORY
    if not frappe.db.has_column("Expense Category", "has_item_list"):
        return category in ITEM_LIST_EXPENSE_CATEGORIES
    return bool(frappe.db.get_value("Expense Category", category, "has_item_list"))


def category_is_fixed_asset(category):
    """Whether a supplier-bill line becomes an ERPNext Asset at approval."""
    if not category:
        return False
    if not frappe.db.has_column("Expense Category", "is_fixed_asset"):
        return category in FIXED_ASSET_PURCHASE_CATEGORIES
    return bool(frappe.db.get_value("Expense Category", category, "is_fixed_asset"))


def category_asset_category(category):
    """The ERPNext Asset Category mapped to a purchase category."""
    if not category_is_fixed_asset(category):
        return None
    if frappe.db.has_column("Expense Category", "asset_category"):
        mapped = frappe.db.get_value("Expense Category", category, "asset_category")
        if mapped:
            return mapped
    return FIXED_ASSET_PURCHASE_CATEGORIES.get(category)


def _expense_approval(doc):
    """Return the approval linked to an expense, if it has one."""
    if not frappe.db.exists("DocType", "POS Approval"):
        return None
    fields = ["name", "action_payload", "resolved_by", "resolved_at"]
    if frappe.db.has_column("POS Approval", "manager_comment"):
        fields.append("manager_comment")
    for approval in frappe.get_all(
            "POS Approval",
            filters={"action": "expense", "action_payload": ["like", f"%{doc.name}%"]},
            fields=fields, order_by="resolved_at desc", limit_page_length=10):
        try:
            linked = json.loads(approval.action_payload or "{}").get("expense")
        except (TypeError, ValueError):
            continue
        if linked == doc.name:
            return approval
    return None


def _reclassified_expense_money(doc, approval):
    """Original subtotal/discount when a stock bill became this expense."""
    if not approval:
        return 0.0, 0.0, ""
    try:
        payload = json.loads(approval.action_payload or "{}")
    except (TypeError, ValueError):
        payload = {}
    source = payload.get("purchase_bill") or ""
    if not source and frappe.db.exists("DocType", "LF Activity Log"):
        rows = frappe.get_all(
            "LF Activity Log",
            filters={"action": "purchase_bill.reclassified", "ref_name": doc.name},
            fields=["detail"], order_by="creation desc", limit_page_length=1,
        )
        if rows:
            try:
                source = json.loads(rows[0].detail or "{}").get("purchase_bill") or ""
            except (TypeError, ValueError):
                source = ""
    if not source or not frappe.db.exists("Purchase Bill", source):
        return 0.0, float(payload.get("discount") or 0), source
    bill = frappe.get_doc("Purchase Bill", source)
    subtotal = sum(float(line.qty or 0) * float(line.price or 0)
                   for line in (bill.get("items") or []))
    return subtotal, float(bill.get("discount") or payload.get("discount") or 0), source


def _expense_resolution(doc, approval=None):
    """Who decided an expense and when, including pre-audit-field records."""
    resolved_by = doc.get("resolved_by") or ""
    resolved_at = doc.get("resolved_at")
    if (doc.status or "pending") == "pending" or (resolved_by and resolved_at):
        return resolved_by, resolved_at
    approval = approval or _expense_approval(doc)
    if approval:
        return resolved_by or approval.resolved_by or "", resolved_at or approval.resolved_at
    return resolved_by, resolved_at


def _expense_dict(name):
    d = frappe.get_doc("Expense", name)
    approval = _expense_approval(d)
    resolved_by, resolved_at = _expense_resolution(d, approval)
    source_subtotal, source_discount, source_bill = _reclassified_expense_money(d, approval)
    return {
        "name": d.name, "category": d.category, "payee": d.payee or "",
        "amount": float(d.amount or 0), "date": str(d.expense_date or ""),
        "bill_ref": d.bill_ref or "", "note": d.note or "",
        "receipt_image": d.receipt_image or "", "outlet": d.pos_profile or "",
        "status": d.status, "shift": d.pos_opening_entry or "",
        "by": frappe.get_value("User", d.requested_by, "full_name") or d.requested_by or "",
        "when": frappe.utils.pretty_date(d.requested_at) if d.requested_at else "",
        "resolved_by": (frappe.get_value("User", resolved_by, "full_name")
                        or resolved_by or "") if resolved_by else "",
        "resolved_time": frappe.utils.pretty_date(resolved_at) if resolved_at else "",
        "resolved_at": str(resolved_at or ""),
        "resolved_when": (frappe.utils.format_datetime(resolved_at, "d MMM yyyy, h:mm a")
                          if resolved_at else ""),
        "manager_comment": approval.get("manager_comment") if approval else "",
        "source_subtotal": source_subtotal,
        "discount": source_discount,
        "source_purchase_bill": source_bill,
    }
@frappe.whitelist(methods=["POST"])
def create_expense():
    """Record an expense and route it to the manager for approval.

    Deliberately does NOT write a POS Cash Movement. The drawer's expected cash is computed in two
    places that must agree — the POS close dialog (client-side) and ERPNext's expected_amount — and
    neither accounts for pay-outs. Subtracting an expense in only one of them is worse than in
    neither, so an expense is a record, not a till movement. The shift is stored for context only.
    """
    roles.require_expenses()
    if not frappe.db.exists("DocType", "Expense"):
        _install_expenses()
    data = frappe.local.form_dict

    category = (data.get("category") or "").strip()
    if not category or not frappe.db.exists("Expense Category", category):
        frappe.throw(_("Pick a category"))
    if category_is_fixed_asset(category):
        frappe.throw(_("Fixed assets must be entered as an itemised Purchase Bill"))
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
        _install_pos_approval()
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
@frappe.whitelist(methods=["POST"])
def update_expense(name=None, category=None, payee=None, expense_date=None,
                   receipt_image=None, note=None):
    """Correct an expense's category, payee, date, photo or note. Manager,
    owner or admin, at any stage. The amount is what was approved, so it is not
    editable here — see purchases.update_purchase_bill for the same rule.

    A field left out of the request is left alone; an empty payee, photo or
    note clears it."""
    from frappe.utils import getdate, today
    from luuvcrm import activity
    from luuvcrm.api.purchases import _find_pending_card, _visible

    roles.require_manager()
    doc = _visible("Expense", name)
    before = {"category": doc.category, "payee": doc.payee or "",
              "expense_date": str(doc.expense_date or ""),
              "receipt_image": doc.receipt_image or "", "note": doc.note or ""}
    after = dict(before)
    if category is not None:
        category = category.strip()
        if not category or not frappe.db.exists("Expense Category", category):
            frappe.throw(_("Pick a category"))
        if category_is_fixed_asset(category):
            frappe.throw(_("Fixed assets must be entered as an itemised Purchase Bill"))
        after["category"] = category
    if payee is not None:
        after["payee"] = payee.strip()
    if expense_date is not None and str(expense_date) != before["expense_date"]:
        try:
            day = getdate(expense_date)
        except Exception:
            frappe.throw(_("That is not a date: {0}").format(expense_date))
        if day > getdate(today()):
            frappe.throw(_("The date cannot be in the future"))
        after["expense_date"] = str(day)
    if receipt_image is not None:
        after["receipt_image"] = receipt_image.strip()
    if note is not None:
        after["note"] = note.strip()

    changed = {k: v for k, v in after.items() if v != before[k]}
    if not changed:
        return {"ok": True, "changed": [], "expense": _expense_dict(doc.name)}
    for field, value in changed.items():
        doc.db_set(field, value or None, update_modified=True)

    card = _find_pending_card("expense", "expense", doc.name)
    if card and ("category" in changed or "payee" in changed):
        frappe.db.set_value("POS Approval", card, {
            "title": after["category"],
            "subhead": (after["payee"] + " · " if after["payee"] else "") + "expense"})

    activity.log_activity(
        "expense.edit", label="Expense corrected: %s" % after["category"],
        ref_doctype="Expense", ref_name=doc.name, amount=doc.amount,
        detail={"from": {k: before[k] for k in changed}, "to": changed})
    return {"ok": True, "changed": sorted(changed), "expense": _expense_dict(doc.name)}


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
    roles.require_expenses()
    if not frappe.db.exists("DocType", "Expense"):
        return {"expenses": [], "approved_today": 0, "pending_amt": 0, "week": 0}
    rows = frappe.get_list("Expense", filters={branch.FIELD: branch.viewing()},
                          fields=["name"], order_by="requested_at desc",
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
