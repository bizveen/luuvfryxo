"""The profit-and-loss statement: what the manager hands the owner or an accountant.

One page: every cost line, every income line, the profit. Built from the same
sources as the rest of the Reports tab — approved Expenses, approved Purchase
Bills, POS Invoices — so a figure here can be found again on a card there.

Three decisions worth knowing before changing anything:

* **Food cost is the Food / Ingredients share of approved purchase bills.**
  Wastage is not added: a spoiled item was bought on a bill already counted,
  and adding its cost again counts the same money twice. Non-food lines on the
  same receipt flow to their own P&L categories. It is still the money spent
  buying food in the period, not the food used.
* **Food and Beverages are told apart by Kitchen Group** — Beverages and
  Coffee & Tea are drinks, everything else is food — because that is the split
  the kitchen already keeps. An item with no group is food.
* **Total sales is the invoices' grand total**, the same number as Total revenue
  on the Reports tab. The item lines do not add up to it on their own (service
  charge sits in a tax row, a discount in none), so those two are shown as their
  own lines rather than folded into food or drink.
"""

import frappe
from frappe import _
from luuvcrm import branch, roles, settings
from luuvcrm.api.expenses import FOOD_PURCHASE_CATEGORY, category_is_fixed_asset
from luuvcrm.api.reports import _report_range

# The statement's cost lines, in the order the owner's template has them, each
# with the Expense Categories that feed it. Anything not named here — including a
# category a manager added later — lands in "Other cost", so the lines always add
# up to what was actually spent.
COST_LINES = [
    ("Rent", ("Rent",)),
    ("Payroll", ("Salaries/Wages",)),
    ("Advertising", ("Advertising",)),
    ("Internet and phone", ("Internet/Phone",)),
    ("Repairs and maintenance", ("Repairs & Maintenance",)),
    ("Office supplies", ("Office Supplies",)),
    ("Packaging", ("Packaging",)),
    ("Transport", ("Transport/Fuel",)),
    ("Employee houses rent", ("Employee Houses Rent",)),
    ("Staff meals", ("Staff Meals",)),
    ("Staff medical", ("Staff Medical",)),
    ("Municipal council", ("Municipal Council",)),
    ("Water bill", ("Water",)),
    ("Electricity bill", ("Electricity",)),
    ("Taxes", ("Taxes",)),
    ("Utilities (gas)", ("Gas (LP)",)),
    ("Charities", ("Charities",)),
]

#: Kitchen Groups that are drinks. Everything else the till sells is food.
BEVERAGE_GROUPS = ("Beverages", "Coffee & Tea")

#: The two costs a month is not a month without. Checked only over a window long
#: enough to be expected to hold them.
FIXED_LINES = ("Rent", "Payroll")
FIXED_WINDOW_DAYS = 28


def _sum_expenses(start, end, status):
    """{category: amount} of Expenses with this status dated in the window."""
    if not frappe.db.exists("DocType", "Expense"):
        return {}
    cond, params = branch.sql_restaurant()
    rows = frappe.db.sql(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS amount
        FROM `tabExpense`
        WHERE status = %s
          AND COALESCE(expense_date, DATE(creation)) BETWEEN %s AND %s
        """ + cond + """
        GROUP BY category
        """, (status, start, end) + params, as_dict=True)
    return {r.category: float(r.amount or 0) for r in rows}


def _purchase_costs_by_category(start, end):
    """Approved supplier-bill spend allocated to its child-line categories."""
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return {}
    cond, params = branch.sql_restaurant("pb")
    if not frappe.db.has_column("Purchase Bill Item", "category"):
        amount = float(frappe.db.sql(
            """
            SELECT COALESCE(SUM(pb.total), 0) FROM `tabPurchase Bill` pb
            WHERE pb.status = 'approved'
              AND pb.purchase_date >= %s AND pb.purchase_date < %s
            """ + cond,
            (str(start), str(frappe.utils.add_days(end, 1))) + params,
        )[0][0] or 0)
        return {FOOD_PURCHASE_CATEGORY: amount} if amount else {}
    rows = frappe.db.sql(
        """
        SELECT COALESCE(NULLIF(pbi.category, ''), %s) AS category,
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
        WHERE pb.status = 'approved'
          AND pb.purchase_date >= %s AND pb.purchase_date < %s
        """ + cond + " GROUP BY category",
        (FOOD_PURCHASE_CATEGORY, str(start), str(frappe.utils.add_days(end, 1))) + params,
        as_dict=True,
    )
    # Asset purchases stay visible in Purchases but belong on the balance sheet,
    # not in this period's operating profit.
    out = {r.category: float(r.amount or 0) for r in rows
           if not category_is_fixed_asset(r.category)}
    # Defensive legacy fallback: very old/imported bills can have a header
    # total but no child rows. They were historically all counted as food.
    orphan = float(frappe.db.sql(
        """
        SELECT COALESCE(SUM(pb.total), 0) FROM `tabPurchase Bill` pb
        WHERE pb.status = 'approved'
          AND pb.purchase_date >= %s AND pb.purchase_date < %s
          AND NOT EXISTS (SELECT 1 FROM `tabPurchase Bill Item` pbi
                          WHERE pbi.parent = pb.name)
        """ + cond,
        (str(start), str(frappe.utils.add_days(end, 1))) + params,
    )[0][0] or 0)
    if orphan:
        out[FOOD_PURCHASE_CATEGORY] = out.get(FOOD_PURCHASE_CATEGORY, 0.0) + orphan
    return out


def _food_cost(start, end):
    """Food/ingredient lines from approved supplier bills in the window."""
    return _purchase_costs_by_category(start, end).get(FOOD_PURCHASE_CATEGORY, 0.0)


def _sales(start, end):
    """(total, food, beverages, service_and_charges) over the window."""
    cond, params = branch.sql_cost_center("pi")
    total = float(frappe.db.sql(
        """
        SELECT COALESCE(SUM(pi.grand_total), 0) FROM `tabPOS Invoice` pi
        WHERE pi.docstatus = 1 AND pi.posting_date BETWEEN %s AND %s
        """ + cond, (start, end) + params)[0][0] or 0)

    charges = float(frappe.db.sql(
        """
        SELECT COALESCE(SUM(t.tax_amount), 0) FROM `tabSales Taxes and Charges` t
        INNER JOIN `tabPOS Invoice` pi ON pi.name = t.parent AND pi.docstatus = 1
        WHERE t.parenttype = 'POS Invoice' AND pi.posting_date BETWEEN %s AND %s
        """ + cond, (start, end) + params)[0][0] or 0)

    # A site without the Kitchen Group field sells everything as food rather than
    # failing: the statement is still right in total.
    grouped = (frappe.db.exists("DocType", "Kitchen Group")
               and frappe.db.has_column("Item", "kitchen_group"))
    if grouped:
        join = "LEFT JOIN `tabKitchen Group` kg ON kg.name = it.kitchen_group"
        kind = "CASE WHEN kg.group_name IN %s THEN 'beverage' ELSE 'food' END"
        extra = (BEVERAGE_GROUPS,)
    else:
        join, kind, extra = "", "'food'", ()
    rows = frappe.db.sql(
        f"""
        SELECT {kind} AS kind, COALESCE(SUM(soi.rate * soi.qty), 0) AS amount
        FROM `tabPOS Order Item` soi
        INNER JOIN `tabPOS Order` so ON so.name = soi.parent
        INNER JOIN `tabPOS Invoice` pi ON pi.name = so.pos_invoice AND pi.docstatus = 1
        LEFT JOIN `tabItem` it ON it.name = soi.item
        {join}
        WHERE pi.posting_date BETWEEN %s AND %s
        """ + cond + """
        GROUP BY kind
        """, extra + (start, end) + params, as_dict=True)
    by_kind = {r.kind: float(r.amount or 0) for r in rows}
    return total, by_kind.get("food", 0.0), by_kind.get("beverage", 0.0), charges


def _period_label(start, end):
    from frappe.utils import formatdate, get_last_day, getdate
    s, e = getdate(start), getdate(end)
    if s.day == 1 and e == get_last_day(s):
        return formatdate(s, "MMMM yyyy")
    return f"{formatdate(s, 'd MMM yyyy')} – {formatdate(e, 'd MMM yyyy')}"


@frappe.whitelist()
def get_manager_pnl(days=30, start=None, end=None):
    """The statement as data. `start`/`end` (a picked month) win over `days`."""
    roles.require_manager()
    start, end, days = _report_range(days, start, end, cap=366)

    approved = _sum_expenses(start, end, "approved")
    purchase_costs = _purchase_costs_by_category(start, end)
    food_cost = (purchase_costs.pop(FOOD_PURCHASE_CATEGORY, 0.0)
                 + approved.pop(FOOD_PURCHASE_CATEGORY, 0.0))
    # Non-food lines on a mixed receipt join the same statement categories as
    # standalone Expenses. A Food City bill can therefore feed Food cost,
    # Staff meals and Office supplies without duplicating the receipt.
    for category, amount in purchase_costs.items():
        approved[category] = approved.get(category, 0.0) + amount
    costs, mapped = [], set()
    for label, categories in COST_LINES:
        mapped.update(categories)
        costs.append({"label": label, "amount": round(sum(approved.get(c, 0) for c in categories), 2)})
    costs.append({"label": "Food cost", "amount": round(food_cost, 2)})
    costs.append({"label": "Other cost", "amount": round(
        sum(v for c, v in approved.items() if c not in mapped), 2)})
    total_cost = round(sum(c["amount"] for c in costs), 2)

    total, food, drink, charges = _sales(start, end)
    income = [
        {"label": "Food sales", "amount": round(food, 2)},
        {"label": "Beverages sales", "amount": round(drink, 2)},
        {"label": "Other income", "amount": round(charges, 2),
         "note": "service charge and other charges"},
    ]
    # What the item lines and the charges do not explain — order discounts and
    # rounding. Only shown when there is something to show, so the lines always
    # add up to Total sales without a line of noise on a clean month.
    rest = round(total - food - drink - charges, 2)
    if abs(rest) >= 0.01:
        income.append({"label": "Discounts and adjustments", "amount": rest})
    total_sales = round(sum(i["amount"] for i in income), 2)

    profit = round(total_sales - total_cost, 2)

    missing = []
    if days >= FIXED_WINDOW_DAYS:
        pending = _sum_expenses(start, end, "pending")
        by_label = {c["label"]: c["amount"] for c in costs}
        for label, categories in COST_LINES:
            if label in FIXED_LINES and not by_label[label]:
                missing.append({"label": label,
                                "pending": any(pending.get(c, 0) for c in categories)})

    return {
        "brand": settings.get("brand_name"),
        "outlet": settings.get("outlet_line"),
        "start": str(start), "end": str(end), "days": days,
        "period": _period_label(start, end),
        "generated": frappe.utils.format_datetime(frappe.utils.now_datetime(), "d MMM yyyy, HH:mm"),
        "costs": costs, "total_cost": total_cost,
        "income": income, "total_sales": total_sales,
        "profit": profit,
        "profit_pct": round(profit * 100.0 / total_sales, 1) if total_sales > 0 else None,
        "missing_fixed": missing,
        "food_cost_basis": "Food cost is the Food / Ingredients lines from approved purchase "
                           "bills dated in the period — what was bought, not what was used.",
    }


# ─── The printed page ────────────────────────────────────────────────────────

def _money(v):
    return f"{v:,.2f}"


def _row(label, amount, cls="", note=""):
    esc = frappe.utils.escape_html
    sub = f'<span class="sub">{esc(note)}</span>' if note else ""
    return (f'<tr class="{cls}"><td>{esc(label)}{sub}</td>'
            f'<td class="n">{_money(amount)}</td></tr>')


def pnl_html(d, toolbar=False, pdf_url=""):
    """The statement as one self-contained page: print it, or hand it to wkhtmltopdf.

    `toolbar` adds the Print / Download strip and the missing-expense note, both
    for the manager's screen only — neither belongs on the sheet that is handed on.
    """
    esc = frappe.utils.escape_html
    loss = d["profit"] < 0
    parts = []
    if toolbar:
        note = ""
        if d["missing_fixed"]:
            items = ", ".join(
                esc(m["label"]) + (" (waiting for approval)" if m["pending"] else "")
                for m in d["missing_fixed"])
            note = (f'<div class="note">No approved {items} for this period. If it was paid, '
                    f'enter it under Expenses first — the statement will show a profit it did not make.</div>')
        link = f'<a href="{esc(pdf_url)}">Download PDF</a>' if pdf_url else ""
        parts.append(f'<div class="bar no-print"><button onclick="window.print()">Print / Save as PDF</button>'
                     f'{link}</div>{note}')
    title = f'{esc(d["brand"] or "")} {esc(d["outlet"] or "")}'.strip()
    parts.append(f'<h1>Profit and loss statement</h1><h2>{title}</h2>'
                 f'<p class="period">{esc(d["period"])}</p>')
    body = ['<tr class="head"><td colspan="2">Cost and expenses</td></tr>']
    body += [_row(c["label"], c["amount"]) for c in d["costs"]]
    body.append(_row("TOTAL COST", d["total_cost"], "total"))
    body.append('<tr class="head"><td colspan="2">Income and sales</td></tr>')
    body += [_row(i["label"], i["amount"], note=i.get("note", "")) for i in d["income"]]
    body.append(_row("TOTAL SALES", d["total_sales"], "total"))
    body.append(_row("PROFIT" if not loss else "LOSS", abs(d["profit"]),
                     "result loss" if loss else "result"))
    parts.append('<table>' + "".join(body) + '</table>')
    if d["profit_pct"] is not None:
        parts.append(f'<p class="pct">{"Loss" if loss else "Profit margin"}: '
                     f'{abs(d["profit_pct"])}% of sales</p>')
    parts.append(f'<p class="foot">{esc(d["food_cost_basis"])}<br>Amounts in Rs · generated {esc(d["generated"])}</p>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Profit and loss {esc(d["period"])}</title><style>
body{{font-family:Arial,Helvetica,sans-serif;color:#1c1917;max-width:680px;margin:0 auto;padding:24px 18px;}}
h1{{font-size:22px;margin:0;text-align:center}} h2{{font-size:17px;font-weight:600;margin:4px 0 0;text-align:center}}
.period{{text-align:center;color:#57534e;margin:6px 0 20px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td{{padding:7px 8px;border-bottom:1px solid #e7e5e4}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.sub{{display:block;font-size:11px;color:#78716c}}
tr.head td{{background:#f5f5f4;font-weight:700;padding-top:10px}}
tr.total td{{font-weight:700;border-top:2px solid #1c1917}}
tr.result td{{font-weight:800;font-size:16px;border-top:2px solid #1c1917;color:#15803d}}
tr.loss td{{color:#b91c1c}}
.pct{{text-align:right;font-size:13px;color:#57534e}} .foot{{font-size:11px;color:#78716c;margin-top:22px}}
.bar{{display:flex;gap:14px;align-items:center;margin-bottom:14px}}
.bar button{{padding:9px 16px;font-size:14px;font-weight:700;border:0;border-radius:8px;background:#F15207;color:#fff}}
.bar a{{font-size:14px;font-weight:700}}
.note{{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:16px}}
@media print{{.no-print{{display:none}} body{{padding:0}}}}
</style></head><body>{"".join(parts)}</body></html>"""


@frappe.whitelist()
def print_manager_pnl(days=30, start=None, end=None, format="html"):
    """The statement as a page to print (`html`) or a file to keep (`pdf`)."""
    roles.require_manager()
    d = get_manager_pnl(days=days, start=start, end=end)
    name = f"profit-and-loss-{d['start']}-to-{d['end']}"
    if format == "pdf":
        from frappe.utils.pdf import get_pdf
        try:
            pdf = get_pdf(pnl_html(d))
        except OSError:
            # wkhtmltopdf is not on every server; the page still prints.
            frappe.throw(_("PDF files are not available on this server. Open the statement and use Print → Save as PDF."))
        frappe.local.response.update({
            "type": "download", "filename": name + ".pdf", "filecontent": pdf,
            "content_type": "application/pdf", "display_content_as": "attachment"})
        return
    from urllib.parse import urlencode
    query = urlencode({k: v for k, v in (("days", days), ("start", start), ("end", end)) if v} | {"format": "pdf"})
    frappe.local.response.update({
        "type": "download", "filename": name + ".html",
        "filecontent": pnl_html(d, toolbar=True,
                                pdf_url="/api/method/luuvcrm.api.print_manager_pnl?" + query),
        "content_type": "text/html", "display_content_as": "inline"})
