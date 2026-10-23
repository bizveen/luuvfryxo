"""Buying: vendor bills, the approval they need, and the price book they write.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import activity, branch, roles

from luuvcrm.api.schema import _install_pos_approval


# ─── Purchases & Expenses (buying bills → manager approval → price book) ──
# A purchaser logs a supplier bill (items + purchase prices + receipt); the manager approves,
# which updates each item's BUYING Item Price (the "price book"). Frappe-default = a DocType.

def _buying_price_list():
    pl = frappe.db.get_value("Price List", {"buying": 1, "enabled": 1}, "name")
    return pl or "Standard Buying"
def _install_purchase_bill():
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
            {"fieldname": "purchase_date", "label": "Purchase Date", "fieldtype": "Date",
             "in_list_view": 1, "search_index": 1},
            {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
            {"fieldname": "receipt_image", "label": "Receipt", "fieldtype": "Attach Image"},
            {"fieldname": "discount", "label": "Discount", "fieldtype": "Currency", "default": "0"},
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
@frappe.whitelist()
def setup_purchase_bill():
    """Schema setup over HTTP. Admin only; the work is in _install_purchase_bill."""
    frappe.only_for("System Manager")
    return _install_purchase_bill()
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
    frappe.only_for("System Manager")
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
    roles.require_purchases()
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
    roles.require_purchases()
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
@frappe.whitelist(methods=["POST"])
def rename_purchase_item():
    """Fix a purchase item's display name (and unit) from the price book.

    Only the display name (`item_name`) and the unit (`stock_uom`) change — the
    item_code, the price book entry and every existing bill's link stay put, so
    nothing is re-priced or orphaned. The correction shows in the price book, the
    picker and on every bill raised from now on; bills already raised keep the
    name and unit they were saved with, because their line is a snapshot.
    """
    roles.require_purchases()
    data = frappe.local.form_dict
    item = (data.get("item") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not item or not new_name:
        frappe.throw(_("Item and a new name are required"))
    if not frappe.db.exists("Item", item):
        frappe.throw(_("Item not found"))
    # Another item already carries this exact name — renaming into it would make
    # the picker ambiguous, since create_purchase_item dedupes on item_name.
    clash = frappe.db.get_value("Item", {"item_name": new_name, "name": ["!=", item]}, "name")
    if clash:
        frappe.throw(_("Another item is already called '{0}'").format(new_name))
    was = frappe.db.get_value("Item", item, "item_name")
    frappe.db.set_value("Item", item, "item_name", new_name)
    if unit:
        if not frappe.db.exists("UOM", unit):
            frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert(ignore_permissions=True)
        frappe.db.set_value("Item", item, "stock_uom", unit)

    activity.log_activity(
        "item.rename", label="%s → %s" % (was or item, new_name),
        ref_doctype="Item", ref_name=item, item=item, item_name=new_name,
        detail={"was": was, "now": new_name, "unit": unit or None})
    return {"ok": True, "item": item, "name": new_name,
            "unit": unit or frappe.db.get_value("Item", item, "stock_uom")}


#: How far back a bill may be dated. A month covers a bill found in a drawer;
#: anything older is far likelier a slip of the date picker (last year for this
#: year), and it would quietly rewrite a month whose P&L has already been handed on.
MAX_BACKDATE_DAYS = 30


def _purchase_date(value=None):
    """The day the goods were bought, as the buyer entered it. Blank is today.

    Not in the future: a bill from tomorrow has not happened, and a slip of the
    date picker would otherwise park real spend in a week nobody is looking at.
    Not more than MAX_BACKDATE_DAYS back either, for the same reason."""
    from frappe.utils import add_days, getdate, today
    if not value:
        return getdate(today())
    try:
        day = getdate(value)
    except Exception:
        frappe.throw(_("That is not a date: {0}").format(value))
    if day > getdate(today()):
        frappe.throw(_("The purchase date cannot be in the future"))
    if day < getdate(add_days(today(), -MAX_BACKDATE_DAYS)):
        frappe.throw(_("The purchase date cannot be more than {0} days ago").format(MAX_BACKDATE_DAYS))
    return day


# ─── Vendors ──────────────────────────────────────────────────────────────
# Vendor is free text on the bill, so "Green Valley", "green valley " and
# "Green  Valley" were three vendors in every report. The sheet now offers the
# names already used as the buyer types, and a name that differs from one of
# them only in case or spacing is saved as that one. A genuinely new name is
# still just typed — there is no vendor master to maintain.

def _vendor_key(name):
    """What two spellings of one vendor have in common: spacing and case gone."""
    return " ".join((name or "").split()).lower()


def _known_vendors():
    """Every vendor this restaurant has billed, most-used first, one row per
    vendor however many ways it has been spelt (the commonest spelling wins)."""
    cond, params = branch.sql_restaurant()
    rows = frappe.db.sql("""
        SELECT vendor, COUNT(*) AS n, MAX(purchase_date) AS last
        FROM `tabPurchase Bill`
        WHERE IFNULL(vendor, '') <> ''
        """ + cond + """
        GROUP BY BINARY vendor
    """, params, as_dict=True)
    merged = {}
    for r in rows:
        key = _vendor_key(r.vendor)
        if not key:
            continue
        spelling = " ".join(r.vendor.split())
        v = merged.setdefault(key, {"name": spelling, "count": 0, "last": None, "_best": 0})
        v["count"] += int(r.n or 0)
        if int(r.n or 0) > v["_best"]:
            v["name"], v["_best"] = spelling, int(r.n or 0)
        if r.last and (v["last"] is None or r.last > v["last"]):
            v["last"] = r.last
    out = [{"name": v["name"], "count": v["count"], "last": str(v["last"] or "")}
           for v in merged.values()]
    return sorted(out, key=lambda v: (-v["count"], v["name"].lower()))


def _canonical_vendor(typed):
    """The existing spelling of a vendor the buyer has typed, or the typed name
    tidied of stray spaces if it is a new one."""
    tidy = " ".join((typed or "").split())
    key = _vendor_key(tidy)
    for v in _known_vendors():
        if _vendor_key(v["name"]) == key:
            return v["name"]
    return tidy


@frappe.whitelist()
def get_purchase_vendors():
    """Vendors for the new-bill sheet's suggestions, most-used first."""
    roles.require_purchases()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"vendors": []}
    return {"vendors": _known_vendors()}


def _bill_dict(name):
    doc = frappe.get_doc("Purchase Bill", name)
    lines = [{"item": i.item, "name": i.item_name or i.item, "unit": i.uom or "Nos",
              "qty": float(i.qty or 0), "price": float(i.price or 0),
              "old_price": float(i.old_price or 0)} for i in (doc.get("items") or [])]
    subtotal = sum(l["qty"] * l["price"] for l in lines)
    # `total` is what the restaurant actually owes, so it is the one the day
    # summary adds up and the food-cost report sums. The lines' own arithmetic
    # is `subtotal`, kept separate so a screen can show both and neither has to
    # be worked out twice in JavaScript.
    discount = float(doc.get("discount") or 0)
    total = max(subtotal - discount, 0)
    # A bill counts as "due" (owed to the vendor) once it is approved but not yet
    # marked paid. payment_status is a custom field added by setup_purchase_payment_fields;
    # .get() keeps this safe if the column hasn't been created yet (reads as None → unpaid).
    pay_status = doc.get("payment_status") or "unpaid"
    return {
        "name": doc.name, "vendor": doc.vendor or "", "note": doc.note or "",
        "receipt_image": doc.receipt_image or "", "status": doc.status or "pending",
        "by": frappe.get_value("User", doc.requested_by, "full_name") or doc.requested_by or "",
        "time": frappe.utils.pretty_date(doc.requested_at) if doc.requested_at else "",
        # The day it was bought, which is what every report counts it under.
        # `time` stays as when it was typed in.
        "purchase_date": str(doc.get("purchase_date") or ""),
        "date": (frappe.utils.formatdate(doc.purchase_date, "d MMM yyyy")
                 if doc.get("purchase_date") else ""),
        # who settled it, for the grouped Bills list and the drill-down sheet
        "resolved_by": (frappe.get_value("User", doc.resolved_by, "full_name")
                        or doc.resolved_by or "") if doc.get("resolved_by") else "",
        "resolved_time": frappe.utils.pretty_date(doc.resolved_at) if doc.get("resolved_at") else "",
        "lines": lines, "subtotal": subtotal, "discount": discount, "total": total,
        "payment_status": pay_status,
        "due": (doc.status == "approved" and pay_status != "paid"),
        "paid_at": str(doc.get("paid_at")) if doc.get("paid_at") else "",
    }
@frappe.whitelist(methods=["POST"])
def create_purchase_bill():
    roles.require_purchases()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        _install_purchase_bill()
    data = frappe.local.form_dict
    vendor = _canonical_vendor(data.get("vendor"))
    if not vendor:
        frappe.throw(_("Vendor is required"))
    lines = frappe.parse_json(data.get("lines") or "[]")
    if not lines:
        frappe.throw(_("Add at least one item"))
    day = _purchase_date(data.get("purchase_date"))
    doc = frappe.get_doc({
        "doctype": "Purchase Bill", "vendor": vendor, "purchase_date": day,
        "note": (data.get("note") or "").strip(),
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
        _install_pos_approval()
    n = len(appr_lines)
    # A bill for an earlier day says so on the card, or the approver reads it as
    # today's buying.
    dated = ("" if day == frappe.utils.getdate(frappe.utils.today())
             else " · bought " + frappe.utils.formatdate(day, "d MMM"))
    frappe.get_doc({
        "doctype": "POS Approval", "approval_type": "bill", "title": vendor,
        "subhead": f"{n} item{'' if n == 1 else 's'} · purchase bill{dated}",
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
    roles.require_purchases()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"bills": [], "approved_today": 0, "pending_amt": 0, "week": 0}
    # Listed in the order they were entered, so a bill typed in just now is at
    # the top whatever day it is dated. The figures count by purchase_date.
    rows = frappe.get_list("Purchase Bill", filters={}, fields=["name", "status", "purchase_date"],
                          order_by="requested_at desc", limit_page_length=60)
    bills = [_bill_dict(r.name) for r in rows]
    today = frappe.utils.today()
    wk = frappe.utils.add_days(today, -7)
    approved_today = sum(b["total"] for b, r in zip(bills, rows)
                         if b["status"] == "approved" and str(r.purchase_date or "") >= today)
    pending_amt = sum(b["total"] for b in bills if b["status"] == "pending")
    week = sum(b["total"] for b, r in zip(bills, rows)
               if b["status"] != "rejected" and str(r.purchase_date or "") >= wk)
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
def get_purchase_day_summary(date=None):
    """One day's purchasing at a glance: what was entered, what it cost, where it
    stands, who was involved — and the day's bills themselves.

    Powers the day card above the Bills list in the Manager app, which lists the
    bills grouped by status. The day owns a bill by `purchase_date` (the day the
    goods were bought, as the buyer entered it) — the same day every purchase
    report counts it under — so a bill typed in on Monday for Saturday's market
    run is Saturday's. The numbers add up to that day's buying no matter when a
    manager got round to approving it; `approved_by` therefore names whoever
    resolved *those* bills, which may have happened later. Each bill carries its
    full line items, so tapping one opens the same breakdown sheet as the Bills
    list without a second call.
    """
    from frappe.utils import formatdate, getdate, today as _today

    roles.require_purchases()
    day = getdate(date or _today())
    empty = {"date": str(day), "label": formatdate(day, "EEE, d MMM"),
             "is_today": str(day) == str(getdate(_today())), "bills": [], "count": 0,
             "total": 0, "approved": {"count": 0, "total": 0},
             "pending": {"count": 0, "total": 0}, "rejected": {"count": 0, "total": 0},
             "entered_by": [], "approved_by": []}
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return empty

    names = frappe.get_all("Purchase Bill",
                           filters=[["purchase_date", "=", str(day)]],
                           fields=["name", "requested_by", "resolved_by"],
                           order_by="requested_at desc", limit_page_length=0)
    if not names:
        return empty
    bills = [_bill_dict(r.name) for r in names]

    buckets = {k: {"count": 0, "total": 0.0} for k in ("approved", "pending", "rejected")}
    entered, approved_by = {}, {}
    for r, b in zip(names, bills):
        amount = b["total"]
        bucket = buckets.setdefault(b["status"] or "pending", {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += amount
        row = entered.setdefault(r.requested_by or "", {"count": 0, "total": 0.0})
        row["count"] += 1
        row["total"] += amount
        if b["status"] == "approved" and r.resolved_by:
            row = approved_by.setdefault(r.resolved_by, {"count": 0, "total": 0.0})
            row["count"] += 1
            row["total"] += amount

    def people(d):
        out = [{"user": u, "name": (frappe.get_value("User", u, "full_name") or u or "—"),
                "count": v["count"], "total": round(v["total"], 2)}
               for u, v in d.items()]
        return sorted(out, key=lambda x: -x["total"])

    empty.update({
        "bills": bills,
        "count": len(bills),
        "total": round(sum(b["total"] for b in bills), 2),
        "approved": {"count": buckets["approved"]["count"],
                     "total": round(buckets["approved"]["total"], 2)},
        "pending": {"count": buckets["pending"]["count"],
                    "total": round(buckets["pending"]["total"], 2)},
        "rejected": {"count": buckets["rejected"]["count"],
                     "total": round(buckets["rejected"]["total"], 2)},
        "entered_by": people(entered),
        "approved_by": people(approved_by),
    })
    return empty


@frappe.whitelist()
def search_purchase_bills(q=None, status=None, limit=30, offset=0):
    """Reach past the 60 newest bills the Bills list holds.

    Rows come straight from the DB — vendor, total, status, date — with no items and
    no Document built, so a search costs the same whether the shop has 200 bills or
    20,000. The lines load only when a card is tapped (get_purchase_bill).
    Matches the vendor, the note, or any item name on the bill.
    """
    roles.require_purchases()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"bills": [], "more": False}

    q = (q or "").strip()
    limit = max(1, min(int(limit or 30), 50))
    offset = max(0, int(offset or 0))
    where = ["1 = 1"]
    params = {"lim": limit + 1, "off": offset}   # one extra row answers "is there more?"
    if q:
        params["like"] = "%" + q + "%"
        where.append("""(b.vendor LIKE %(like)s OR b.note LIKE %(like)s
            OR EXISTS (SELECT 1 FROM `tabPurchase Bill Item` i
                       WHERE i.parent = b.name AND i.item_name LIKE %(like)s))""")
    if status in ("pending", "approved", "rejected"):
        params["status"] = status
        where.append("b.status = %(status)s")

    rows = frappe.db.sql("""
        SELECT b.name, b.vendor, b.total, b.status, b.requested_at, b.purchase_date,
               b.receipt_image, b.requested_by
        FROM `tabPurchase Bill` b
        WHERE {where}
        ORDER BY b.requested_at DESC
        LIMIT %(lim)s OFFSET %(off)s
    """.format(where=" AND ".join(where)), params, as_dict=True)

    more = len(rows) > limit
    rows = rows[:limit]

    names = {}
    for r in rows:
        u = r.get("requested_by")
        if u and u not in names:
            names[u] = frappe.get_value("User", u, "full_name") or u

    return {"bills": [{
        "name": r.name, "vendor": r.vendor or "", "total": float(r.total or 0),
        "status": r.status or "pending", "receipt_image": r.receipt_image or "",
        "time": frappe.utils.pretty_date(r.requested_at) if r.requested_at else "",
        "date": frappe.utils.formatdate(r.purchase_date, "d MMM yyyy") if r.purchase_date else "",
        "by": names.get(r.requested_by, ""),
    } for r in rows], "more": more}
@frappe.whitelist()
def get_purchase_bill(name=None):
    """One bill in full. Search results carry no lines — a tapped card fetches them."""
    roles.require_purchases()
    if not name or not frappe.db.exists("Purchase Bill", name):
        frappe.throw(_("Bill not found"))
    return _bill_dict(name)
# ─── Correcting a bill after the fact ─────────────────────────────────────
# A bill typed in a hurry at the market comes back with the vendor misspelt,
# the wrong day, or the photo of a different receipt. A manager (or owner, or
# admin) can put the heading right at any stage — pending, approved or paid.
# The items and the total are not editable here: they are what the price book
# and the approval were decided on, and changing them would rewrite a decision
# after it was made. That is what rejecting and re-entering the bill is for.

def _find_pending_card(action, key, name):
    """The still-pending POS Approval raised for this document, if any."""
    if not frappe.db.exists("DocType", "POS Approval"):
        return None
    for card in frappe.get_all("POS Approval", fields=["name", "action_payload"],
                               filters={"status": "pending", "action": action}):
        try:
            if json.loads(card.action_payload or "{}").get(key) == name:
                return card.name
        except ValueError:
            continue
    return None


def _visible(doctype, name):
    """The document, if this user's branch can see it. A manager pinned to one
    restaurant must not reach another's bills by guessing a name."""
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(_("Bill not found"))
    if not frappe.get_list(doctype, filters={"name": name}, pluck="name", limit_page_length=1):
        frappe.throw(_("Bill not found"))
    return frappe.get_doc(doctype, name)


@frappe.whitelist(methods=["POST"])
def update_purchase_bill(name=None, vendor=None, purchase_date=None, receipt_image=None,
                         note=None):
    """Correct a bill's vendor, date, photo or note. Manager, owner or admin.

    A field left out of the request is left alone; an empty `receipt_image` or
    `note` clears it. A changed date is held to the same window a new bill is
    (_purchase_date), an unchanged one is not — an old bill must still be
    correctable in every other respect."""
    roles.require_manager()
    doc = _visible("Purchase Bill", name)
    before = {"vendor": doc.vendor, "purchase_date": str(doc.get("purchase_date") or ""),
              "receipt_image": doc.receipt_image or "", "note": doc.note or ""}
    after = dict(before)
    if vendor is not None:
        after["vendor"] = _canonical_vendor(vendor)
        if not after["vendor"]:
            frappe.throw(_("Vendor is required"))
    if purchase_date is not None and str(purchase_date) != before["purchase_date"]:
        after["purchase_date"] = str(_purchase_date(purchase_date))
    if receipt_image is not None:
        after["receipt_image"] = receipt_image.strip()
    if note is not None:
        after["note"] = note.strip()

    changed = {k: v for k, v in after.items() if v != before[k]}
    if not changed:
        return {"ok": True, "changed": [], "bill": _bill_dict(doc.name)}
    for field, value in changed.items():
        doc.db_set(field, value or None, update_modified=True)

    # A card still waiting in Approvals shows the vendor and the day — keep it
    # telling the approver the same thing the bill now says.
    card = _find_pending_card("purchase_bill", "bill", doc.name)
    if card and ("vendor" in changed or "purchase_date" in changed):
        n = len(doc.get("items") or [])
        day = frappe.utils.getdate(after["purchase_date"]) if after["purchase_date"] else None
        dated = ("" if not day or day == frappe.utils.getdate(frappe.utils.today())
                 else " · bought " + frappe.utils.formatdate(day, "d MMM"))
        frappe.db.set_value("POS Approval", card, {
            "title": after["vendor"],
            "subhead": f"{n} item{'' if n == 1 else 's'} · purchase bill{dated}"})

    activity.log_activity(
        "purchase_bill.edit", label="Bill corrected: %s" % after["vendor"],
        ref_doctype="Purchase Bill", ref_name=doc.name, amount=doc.total,
        detail={"from": {k: before[k] for k in changed}, "to": changed})
    return {"ok": True, "changed": sorted(changed), "bill": _bill_dict(doc.name)}


@frappe.whitelist()
def get_price_book():
    roles.require_purchases()
    cat = get_purchase_catalog()
    return cat
def _decide_purchase_bill(name, decision, discount=None):
    """Apply a manager decision to a Purchase Bill (idempotent); approving updates
    each item's buying Item Price. Returns the number of prices changed.

    `discount` is what the vendor took off the whole bill, entered by the
    approver at the moment they approve — the point at which somebody is
    actually looking at the paper. It reduces `total` and **nothing else**:

    * the items' buying prices are left alone, because a settlement discount or
      a round-down is not a change in what the item costs. Overwriting the price
      book from one haggle would make the next bill's "was Rs" delta compare a
      normal price against a discounted one and report a rise that never
      happened.
    * `total` is what the day summary adds up and what the food-cost report
      sums, so the saving lands in the numbers without anything else moving.
    """
    doc = frappe.get_doc("Purchase Bill", name)
    if doc.status != "pending":
        return 0
    updated = 0
    if decision == "approved":
        if discount not in (None, ""):
            _apply_bill_discount(doc, discount)
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


def _apply_bill_discount(doc, discount=None):
    """Write the approver's discount onto the bill and reduce what is owed."""
    if not frappe.db.has_column("Purchase Bill", "discount"):
        frappe.throw(_("This site has not run the migrate that adds the discount field"))
    amount = float(discount if discount is not None else 0)
    if amount < 0:
        frappe.throw(_("A discount cannot be negative"))
    subtotal = sum(float(i.qty or 0) * float(i.price or 0) for i in (doc.get("items") or []))
    if amount > subtotal:
        # Otherwise the bill is worth less than nothing and the food cost goes
        # negative, which no report is expecting.
        frappe.throw(_("The discount is more than the bill (Rs {0:,.2f})").format(subtotal))
    doc.db_set("discount", amount)
    doc.db_set("total", max(subtotal - amount, 0))
    return amount
@frappe.whitelist(methods=["POST"])
def resolve_purchase_bill():
    """Somebody holding a purchasing role approves or rejects a bill; approving
    updates each item's buying Item Price. Not their own — block_self_approval.

    A `discount` may come with an approval — what the vendor took off the whole
    bill. It is asked for here rather than at entry because approval is the
    moment somebody is holding the paper and can see the figure written on it.
    """
    roles.require_purchases()
    data = frappe.local.form_dict
    name = data.get("name")
    decision = data.get("decision")
    if decision not in ("approved", "rejected"):
        frappe.throw(_("Decision must be 'approved' or 'rejected'"))
    if frappe.db.get_value("Purchase Bill", name, "status") != "pending":
        frappe.throw(_("Already resolved"))
    roles.block_self_approval(frappe.db.get_value("Purchase Bill", name, "requested_by"))
    updated = _decide_purchase_bill(name, decision, discount=data.get("discount"))
    return {"ok": True, "status": decision, "prices_updated": updated,
            "total": frappe.db.get_value("Purchase Bill", name, "total"),
            "discount": frappe.db.get_value("Purchase Bill", name, "discount")}
# ─── Due bills / pending vendor payments ───────────────────────────
# An approved bill is money owed to the vendor until someone marks it paid. These
# two custom fields turn Purchase Bill into a lightweight payables list; there is
# no GL/accounting behind it (consistent with the rest of this app).

def _install_purchase_payment_fields():
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
def setup_purchase_payment_fields():
    """Schema setup over HTTP. Admin only; the work is in _install_purchase_payment_fields."""
    frappe.only_for("System Manager")
    return _install_purchase_payment_fields()
@frappe.whitelist()
def get_due_bills():
    """Approved bills not yet marked paid — the vendor payables list. Oldest owed
    first (so the most overdue payment is at the top), with a per-vendor summary."""
    roles.require_purchases()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {"bills": [], "total_due": 0, "count": 0, "vendors": []}
    if not frappe.db.has_column("Purchase Bill", "payment_status"):
        _install_purchase_payment_fields()

    # IFNULL, not a plain `!= 'paid'`: a bill created before the payment_status field
    # existed reads NULL, and SQL drops NULL rows from a `!=` comparison — it would be
    # owed money missing from the list. Same predicate the Due badge counts by.
    rows = frappe.db.sql("""
        SELECT name FROM `tabPurchase Bill`
        WHERE status = 'approved' AND IFNULL(payment_status, 'unpaid') != 'paid'
        ORDER BY purchase_date ASC, requested_at ASC LIMIT 2000
    """, as_dict=True)
    # 2000, not 200: the live restaurant owes more than 200 bills (every bill
    # approved before payment_status existed reads as owed), and a cap below
    # the count hid the newest ones from the list that exists to show them.
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
    roles.require_purchases()
    if not frappe.db.has_column("Purchase Bill", "payment_status"):
        _install_purchase_payment_fields()
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
