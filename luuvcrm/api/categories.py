"""Spend by category across itemised purchase bills and simple expenses.

One supplier receipt can contain several kinds of spending. Its child lines
carry those categories, and a whole-bill discount is pro-rated by line value so
the category rows still add up exactly to what was paid. Standalone expenses
join the same buckets; tapping a mixed bucket can therefore list both bills and
expenses while each still opens its normal detail sheet.

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

#: Backward-compatible bucket for a pre-migration line with no category.
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
    merged = {}

    def add(category, kind, count, total):
        row = merged.setdefault(category, {"category": category, "count": 0,
                                            "total": 0.0, "kinds": set()})
        row["count"] += int(count or 0)
        row["total"] += float(total or 0)
        row["kinds"].add(kind)

    if frappe.db.exists("DocType", "Purchase Bill"):
        where, params = _bills_where(start, end)
        if frappe.db.has_column("Purchase Bill Item", "category"):
            for r in frappe.db.sql(f"""
                    SELECT COALESCE(NULLIF(pbi.category, ''), %s) AS category,
                           COUNT(DISTINCT pb.name) AS n,
                           COALESCE(SUM(pbi.qty * pbi.price * (
                               CASE WHEN sub.subtotal > 0
                                    THEN pb.total / sub.subtotal ELSE 1 END
                           )), 0) AS amt
                    FROM `tabPurchase Bill Item` pbi
                    INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
                    INNER JOIN (
                        SELECT parent, SUM(qty * price) AS subtotal
                        FROM `tabPurchase Bill Item` GROUP BY parent
                    ) sub ON sub.parent = pb.name
                    WHERE {where}
                    GROUP BY category""",
                    (PURCHASE_CATEGORY,) + params, as_dict=True):
                add(r.category, "bill", r.n, r.amt)
            orphan = frappe.db.sql(f"""
                SELECT COUNT(*) AS n, COALESCE(SUM(pb.total), 0) AS amt
                FROM `tabPurchase Bill` pb
                WHERE {where}
                  AND NOT EXISTS (SELECT 1 FROM `tabPurchase Bill Item` pbi
                                  WHERE pbi.parent = pb.name)""",
                params, as_dict=True)[0]
            if orphan.n:
                add(PURCHASE_CATEGORY, "bill", orphan.n, orphan.amt)
        else:
            r = frappe.db.sql(f"""
                SELECT COUNT(*) AS n, COALESCE(SUM(pb.total), 0) AS amt
                FROM `tabPurchase Bill` pb WHERE {where}""", params, as_dict=True)[0]
            if r.n:
                add(PURCHASE_CATEGORY, "bill", r.n, r.amt)
    if frappe.db.exists("DocType", "Expense"):
        where, params = _expenses_where(start, end)
        for r in frappe.db.sql(f"""
                SELECT e.category, COUNT(*) AS n, COALESCE(SUM(e.amount), 0) AS amt
                FROM `tabExpense` e WHERE {where}
                GROUP BY e.category""", params, as_dict=True):
            add(r.category, "expense", r.n, r.amt)

    rows = []
    for row in merged.values():
        kinds = row.pop("kinds")
        row["kind"] = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        row["total"] = round(row["total"], 2)
        rows.append(row)
    rows.sort(key=lambda x: -x["total"])
    return {"start": str(start), "end": str(end), "days": days, "categories": rows,
            "total": round(sum(x["total"] for x in rows), 2)}


@frappe.whitelist()
def get_category_bills(category=None, days=7, start=None, end=None):
    """The approved bill allocations and expenses behind one category row."""
    roles.require_manager()
    category = (category or "").strip()
    if not category:
        frappe.throw(_("Which category?"))
    start, end, days = _report_range(days, start, end)
    out = {"category": category, "start": str(start), "end": str(end),
           "kind": "mixed", "count": 0, "total": 0.0, "rows": []}
    found_rows = []
    amounts = []
    kinds = set()
    bills, expenses = [], []

    if frappe.db.exists("DocType", "Purchase Bill"):
        where, params = _bills_where(start, end)
        if frappe.db.has_column("Purchase Bill Item", "category"):
            bills = frappe.db.sql(f"""
                SELECT pb.name, pb.purchase_date AS d, pb.requested_at AS entered,
                       COALESCE(SUM(pbi.qty * pbi.price * (
                           CASE WHEN sub.subtotal > 0
                                THEN pb.total / sub.subtotal ELSE 1 END
                       )), 0) AS amount
                FROM `tabPurchase Bill Item` pbi
                INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
                INNER JOIN (
                    SELECT parent, SUM(qty * price) AS subtotal
                    FROM `tabPurchase Bill Item` GROUP BY parent
                ) sub ON sub.parent = pb.name
                WHERE {where}
                  AND COALESCE(NULLIF(pbi.category, ''), %s) = %s
                GROUP BY pb.name, pb.purchase_date, pb.requested_at
                ORDER BY pb.purchase_date DESC, pb.requested_at DESC""",
                params + (PURCHASE_CATEGORY, category), as_dict=True)
            if category == PURCHASE_CATEGORY:
                bills += frappe.db.sql(f"""
                    SELECT pb.name, pb.purchase_date AS d, pb.requested_at AS entered,
                           pb.total AS amount
                    FROM `tabPurchase Bill` pb
                    WHERE {where}
                      AND NOT EXISTS (SELECT 1 FROM `tabPurchase Bill Item` pbi
                                      WHERE pbi.parent = pb.name)
                    ORDER BY pb.purchase_date DESC, pb.requested_at DESC""",
                    params, as_dict=True)
                bills.sort(key=lambda r: (str(r.d or ""), str(r.entered or "")), reverse=True)
        elif category == PURCHASE_CATEGORY:
            bills = frappe.db.sql(f"""
                SELECT pb.name, pb.purchase_date AS d, pb.requested_at AS entered,
                       pb.total AS amount
                FROM `tabPurchase Bill` pb WHERE {where}
                ORDER BY pb.purchase_date DESC, pb.requested_at DESC""",
                params, as_dict=True)
        else:
            bills = []
        amounts.extend(float(r.amount or 0) for r in bills)
        for r in bills[:SHEET_LIMIT]:
            row = _bill_dict(r.name)
            row.update({"record_kind": "bill", "category_total": float(r.amount or 0),
                        "_sort_date": str(r.d or ""), "_sort_time": str(r.entered or "")})
            found_rows.append(row)
        if bills:
            kinds.add("bill")

    if frappe.db.exists("DocType", "Expense"):
        where, params = _expenses_where(start, end)
        expenses = frappe.db.sql(f"""
            SELECT e.name, e.amount,
                   COALESCE(e.expense_date, DATE(e.creation)) AS d,
                   e.requested_at AS entered
            FROM `tabExpense` e
            WHERE e.category = %s AND {where}
            ORDER BY COALESCE(e.expense_date, DATE(e.creation)) DESC, e.requested_at DESC""",
            (category,) + params, as_dict=True)
        amounts.extend(float(r.amount or 0) for r in expenses)
        for r in expenses[:SHEET_LIMIT]:
            row = _expense_dict(r.name)
            row.update({"record_kind": "expense", "category_total": float(r.amount or 0),
                        "_sort_date": str(r.d or ""), "_sort_time": str(r.entered or "")})
            found_rows.append(row)
        if expenses:
            kinds.add("expense")

    found_rows.sort(key=lambda r: (r["_sort_date"], r["_sort_time"]), reverse=True)
    for row in found_rows:
        row.pop("_sort_date", None)
        row.pop("_sort_time", None)
    out["rows"] = found_rows[:SHEET_LIMIT]
    out["kind"] = (next(iter(kinds)) if len(kinds) == 1 else "mixed")
    out["count"] = len(bills) + len(expenses)
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
