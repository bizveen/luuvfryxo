"""Wastage logging and the food-cost report it feeds.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
from luuvcrm import activity, branch, roles
from luuvcrm.api.reports import _report_range

from luuvcrm.api.purchases import _buying_price_list


# ─── Wastage log + food-cost report + approval comments ─────────────────────
# Wastage = spoiled/discarded raw materials, costed from the buying price book
# (same source as /purchases). No recipe/BOM data exists, so "meal cost" is a
# period-level food-cost-% (purchases + wastage vs POS revenue), not per-dish.

def _install_wastage():
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
@frappe.whitelist()
def setup_wastage():
    """Schema setup over HTTP. Admin only; the work is in _install_wastage."""
    frappe.only_for("System Manager")
    return _install_wastage()
@frappe.whitelist(methods=["POST"])
def create_wastage(item=None, qty=None, reason=""):
    roles.require_pos()
    if not frappe.db.exists("DocType", "Wastage"):
        _install_wastage()
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

    # Stock that leaves without a sale. The cost is what it was bought for, so
    # the row is comparable with a purchase and with a discount.
    activity.log_activity(
        "wastage.create",
        label="Wastage %g x %s, Rs %.0f" % (q, d.get("item_name") or item, float(doc.cost or 0)),
        ref_doctype="Wastage", ref_name=doc.name, item=item,
        item_name=d.get("item_name") or item, qty=q, amount=float(doc.cost or 0),
        detail={"reason": (reason or "").strip() or None, "priced": price > 0})

    return {"ok": True, "name": doc.name, "cost": float(doc.cost or 0),
            "priced": price > 0}
@frappe.whitelist()
def get_wastage(days=7):
    roles.require_pos()
    if not frappe.db.exists("DocType", "Wastage"):
        return {"rows": [], "total": 0, "days": int(days or 7)}
    days = max(1, min(int(days or 7), 90))
    start = frappe.utils.add_days(frappe.utils.today(), -days + 1)
    rows = frappe.get_list("Wastage", filters={"creation": [">=", start]},
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
def get_food_cost_report(days=7, start=None, end=None):
    """Sales vs costs for the range: food cost (approved purchase bills +
    wastage), overheads (approved Expenses — rent/salaries/utilities etc.) and
    the resulting profit, all against POS revenue. Period-level (no recipes/BOM
    → no per-dish costing). The window is the last N days by default, or an
    explicit start/end — a whole calendar month picked in the UI.

    **Every window here is now closed at both ends.** It used to be open — only
    `>= start` — so "the last 7 days" quietly included anything dated in the
    future, and a chosen month would have run to the end of time."""
    roles.require_manager()
    start, end, days = _report_range(days, start, end, cap=365)
    # Wastage's creation is a Datetime; the upper bound is start-of-next-day.
    # Purchases count on purchase_date, the day the goods were bought.
    dt_end = frappe.utils.add_days(end, 1)
    # Raw SQL sees no permission layer, so the branch has to be said out loud —
    # every other manager report does. Empty for an Owner, and on a site with
    # one branch. Purchase Bill, Wastage and Expense carry the branch themselves;
    # the invoice carries it as its cost centre.
    rest_cond, rest_params = branch.sql_restaurant()
    inv_cond, inv_params = branch.sql_cost_center()
    purchases = 0.0
    if frappe.db.exists("DocType", "Purchase Bill"):
        purchases = float(frappe.db.sql("""
            select coalesce(sum(total), 0) from `tabPurchase Bill`
            where status = 'approved' and purchase_date >= %s and purchase_date < %s
        """ + rest_cond, (start, dt_end) + rest_params)[0][0] or 0)
    wastage = 0.0
    if frappe.db.exists("DocType", "Wastage"):
        wastage = float(frappe.db.sql("""
            select coalesce(sum(cost), 0) from `tabWastage`
            where creation >= %s and creation < %s
        """ + rest_cond, (start, dt_end) + rest_params)[0][0] or 0)
    overhead = 0.0
    if frappe.db.exists("DocType", "Expense"):
        overhead = float(frappe.db.sql("""
            select coalesce(sum(amount), 0) from `tabExpense`
            where status = 'approved'
              and coalesce(expense_date, date(creation)) between %s and %s
        """ + rest_cond, (start, end) + rest_params)[0][0] or 0)
    revenue = float(frappe.db.sql("""
        select coalesce(sum(grand_total), 0) from `tabPOS Invoice`
        where docstatus = 1 and posting_date between %s and %s
    """ + inv_cond, (start, end) + inv_params)[0][0] or 0)
    food_cost = round(purchases + wastage, 2)
    profit = round(revenue - food_cost - overhead, 2)
    # No sales, no percentage. It used to be 0, so a week with rent paid and
    # nothing sold read "0% overheads · 0% margin" beside a loss — a card that
    # looked broken. None says what is true: there is nothing to divide by.
    pct = lambda v: round(v * 100.0 / revenue, 1) if revenue > 0 else None
    return {"days": days, "start": str(start), "end": str(end),
            "purchases": round(purchases, 2), "wastage": round(wastage, 2),
            "food_cost": food_cost, "overhead": round(overhead, 2),
            "profit": profit, "revenue": round(revenue, 2),
            "food_cost_pct": pct(food_cost), "wastage_pct": pct(wastage),
            "overhead_pct": pct(overhead), "profit_pct": pct(profit)}
