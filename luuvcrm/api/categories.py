"""Spend by category: purchase bills and expenses side by side.

A purchase bill is one category ("Purchase Bill" — the stock the kitchen
buys); every Expense Category is another. The Manager app's Purchases tab
shows one row each for the period on screen, and tapping a row lists the
bills behind it — each opening the same bill sheet as everywhere else.

Approved only, and dated the way every other report dates them: a bill by
its `purchase_date`, an expense by its `expense_date`. That is what makes a
row here agree with Total spend above it and with Sales vs costs.
"""

import frappe
from frappe import _
from luuvcrm import branch, roles

from luuvcrm.api.expenses import _expense_dict
from luuvcrm.api.purchases import _bill_dict
from luuvcrm.api.reports import _report_range

#: The row that stands for Purchase Bills. Not an Expense Category, so it
#: cannot collide with one a manager creates.
PURCHASE_CATEGORY = "Purchase Bill"

#: A sheet of a hundred bills is already more than anybody scrolls; the
#: count and total still cover every one.
SHEET_LIMIT = 100


def _bills_where(start, end):
    cond, params = branch.sql_restaurant("pb")
    return ("pb.status = 'approved' AND pb.purchase_date BETWEEN %s AND %s" + cond,
            (str(start), str(end)) + params)


def _expenses_where(start, end):
    cond, params = branch.sql_restaurant("e")
    return ("e.status = 'approved' AND COALESCE(e.expense_date, DATE(e.creation)) "
            "BETWEEN %s AND %s" + cond, (str(start), str(end)) + params)


@frappe.whitelist()
def get_category_spend(days=7, start=None, end=None):
    """One row per category with approved spend in the window, biggest first."""
    roles.require_manager()
    start, end, days = _report_range(days, start, end)
    rows = []
    if frappe.db.exists("DocType", "Purchase Bill"):
        where, params = _bills_where(start, end)
        r = frappe.db.sql(f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(pb.total), 0) AS amt
            FROM `tabPurchase Bill` pb WHERE {where}""", params, as_dict=True)[0]
        if r.n:
            rows.append({"category": PURCHASE_CATEGORY, "kind": "bill",
                         "count": int(r.n), "total": float(r.amt or 0)})
    if frappe.db.exists("DocType", "Expense"):
        where, params = _expenses_where(start, end)
        for r in frappe.db.sql(f"""
                SELECT e.category, COUNT(*) AS n, COALESCE(SUM(e.amount), 0) AS amt
                FROM `tabExpense` e WHERE {where}
                GROUP BY e.category""", params, as_dict=True):
            rows.append({"category": r.category, "kind": "expense",
                         "count": int(r.n), "total": float(r.amt or 0)})
    rows.sort(key=lambda x: -x["total"])
    return {"start": str(start), "end": str(end), "days": days, "categories": rows,
            "total": round(sum(x["total"] for x in rows), 2)}


@frappe.whitelist()
def get_category_bills(category=None, days=7, start=None, end=None):
    """The approved bills (or expenses) behind one row of get_category_spend,
    newest first. They come whole, so a tapped one opens without another call."""
    roles.require_manager()
    category = (category or "").strip()
    if not category:
        frappe.throw(_("Which category?"))
    start, end, days = _report_range(days, start, end)
    out = {"category": category, "start": str(start), "end": str(end),
           "kind": "bill" if category == PURCHASE_CATEGORY else "expense",
           "count": 0, "total": 0.0, "rows": []}

    if out["kind"] == "bill":
        if not frappe.db.exists("DocType", "Purchase Bill"):
            return out
        where, params = _bills_where(start, end)
        found = frappe.db.sql(f"""
            SELECT pb.name, pb.total FROM `tabPurchase Bill` pb WHERE {where}
            ORDER BY pb.purchase_date DESC, pb.requested_at DESC""", params, as_dict=True)
        out["rows"] = [_bill_dict(r.name) for r in found[:SHEET_LIMIT]]
        amounts = [r.total for r in found]
    else:
        if not frappe.db.exists("DocType", "Expense"):
            return out
        where, params = _expenses_where(start, end)
        found = frappe.db.sql(f"""
            SELECT e.name, e.amount FROM `tabExpense` e
            WHERE e.category = %s AND {where}
            ORDER BY COALESCE(e.expense_date, DATE(e.creation)) DESC, e.requested_at DESC""",
            (category,) + params, as_dict=True)
        out["rows"] = [_expense_dict(r.name) for r in found[:SHEET_LIMIT]]
        amounts = [r.amount for r in found]

    out["count"] = len(found)
    out["total"] = round(sum(float(a or 0) for a in amounts), 2)
    return out


@frappe.whitelist()
def get_purchase_status_bills(status=None, days=7, start=None, end=None):
    """Bills behind a row in Manager > Purchases > Not in the chart.

    Only pending and rejected belong here: approved bills already have their
    category drill-down above. Return the same whole-row shape as that sheet so
    tapping a result opens the normal bill popup, receipt and audit trail.
    """
    roles.require_manager()
    status = (status or "").strip().lower()
    if status not in ("pending", "rejected"):
        frappe.throw(_("Status must be pending or rejected"))
    start, end, days = _report_range(days, start, end)
    out = {"status": status, "start": str(start), "end": str(end),
           "kind": "bill", "count": 0, "total": 0.0, "rows": []}
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return out

    restaurant_cond, restaurant_params = branch.sql_restaurant("pb")
    found = frappe.db.sql(f"""
        SELECT pb.name, pb.total
        FROM `tabPurchase Bill` pb
        WHERE pb.status = %s
          AND pb.purchase_date BETWEEN %s AND %s{restaurant_cond}
        ORDER BY pb.purchase_date DESC, pb.requested_at DESC
    """, (status, str(start), str(end)) + restaurant_params, as_dict=True)
    out["rows"] = [_bill_dict(row.name) for row in found[:SHEET_LIMIT]]
    out["count"] = len(found)
    out["total"] = round(sum(float(row.total or 0) for row in found), 2)
    return out
