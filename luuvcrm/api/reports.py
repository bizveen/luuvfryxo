"""The manager's numbers: today, the day, the month, the bill, the buying.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
from luuvcrm import branch, roles

from luuvcrm.api import menu
from luuvcrm.api.approvals import _format_approvals, _pending_and_recent_approvals


# ─── A note on limits (P3) ─────────────────────────────────────────────────
# The reads in this module are deliberately unbounded, and must stay that way.
# Every one of them is summed — revenue, the payment mix, the item tally, the
# voids — and a limit on a sum does not make the screen faster, it makes the
# number wrong, quietly and in the restaurant's favour or against it depending
# on the day. What bounds them is the filter: each asks for one date, so the row
# count is one day's trade rather than the table. `test_limits.py` asserts that a
# day with more sales than any ceiling still reports all of them.

# ─── Manager dashboard (today's sales, cash drawer, approvals) ─────

@frappe.whitelist()
def get_manager_dashboard():
    """Aggregate today's POS activity for the Manager phone dashboard (all cashiers)."""
    roles.require_manager()
    from frappe.utils import get_datetime, today as _today
    today = _today()

    invoices = frappe.get_all("POS Invoice",
        filters={"docstatus": 1, "posting_date": today, **branch.invoice_filters()},
        fields=["name", "grand_total", "creation"])
    revenue = sum(float(i.grand_total or 0) for i in invoices)
    sales_count = len(invoices)
    avg = revenue / sales_count if sales_count else 0

    # payment mix + cash sales — one grouped read over the child table, not a
    # full Document per invoice. This screen polls, and on a busy day that loop
    # built every invoice of the day, with its items and taxes, to add up a
    # handful of payment rows.
    invoice_names = [i.name for i in invoices]
    pay = {}
    if invoice_names:
        for r in frappe.get_all("Sales Invoice Payment",
                                filters={"parenttype": "POS Invoice",
                                         "parent": ["in", invoice_names]},
                                fields=["mode_of_payment", "sum(amount) as amount"],
                                group_by="mode_of_payment"):
            # NULL and "" group separately in SQL and both read as "Other"
            mode = r.mode_of_payment or "Other"
            pay[mode] = pay.get(mode, 0) + float(r.amount or 0)
    cash_sales = pay.get("Cash", 0)
    payments = [{"mode": m, "amount": a} for m, a in sorted(pay.items(), key=lambda x: -x[1])]

    # hourly revenue
    hourly = {}
    for inv in invoices:
        h = get_datetime(inv.creation).hour
        hourly[h] = hourly.get(h, 0) + float(inv.grand_total or 0)
    hourly_list = [{"hour": h, "amount": hourly[h]} for h in sorted(hourly.keys())]

    # top items today (from paid POS Orders). `get_list` first, so User
    # Permissions decide which orders this manager may count; the tally then
    # reads the child rows of exactly those, in one query instead of building
    # every order document to add up its lines.
    paid_orders = frappe.get_list("POS Order",
        filters={"pos_invoice": ["!=", ""], "creation": [">=", today]}, pluck="name")
    tally = {}
    if paid_orders:
        for it in frappe.get_all("POS Order Item",
                                 filters={"parent": ["in", paid_orders]},
                                 fields=["item", "item_name", "qty", "rate"]):
            key = it.item_name or it.item
            t = tally.setdefault(key, {"qty": 0, "rev": 0})
            qty = int(it.qty or 0)
            t["qty"] += qty
            t["rev"] += float(it.rate or 0) * qty
    top_items = sorted(
        [{"name": k, "qty": v["qty"], "revenue": v["rev"]} for k, v in tally.items()],
        key=lambda x: -x["revenue"])[:5]

    # voids today
    voids = frappe.get_list("POS Order",
        filters={"docstatus": 2, "kitchen_status": "Cancelled", "modified": [">=", today]},
        fields=["grand_total"])
    void_count = len(voids)
    void_amt = sum(float(v.grand_total or 0) for v in voids)

    # cash drawer — the open shift (any cashier)
    opening = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open"}, ["name", "period_start_date", "user"], as_dict=True)
    float_amt = 0
    cash_in = cash_out = 0
    cash_moves = []
    opened_at = ""
    cashier = ""
    if opening:
        od = frappe.get_doc("POS Opening Entry", opening.name)
        float_amt = sum(float(d.opening_amount or 0) for d in (od.balance_details or []))
        opened_at = str(opening.period_start_date)
        cashier = frappe.get_value("User", opening.user, "full_name") or opening.user
        if frappe.db.exists("DocType", "POS Cash Movement"):
            rows = frappe.get_list("POS Cash Movement",
                filters={"pos_opening_entry": opening.name},
                fields=["direction", "amount", "reason", "cashier", "movement_time"],
                order_by="movement_time desc")
            names = {r.cashier for r in rows if r.cashier}
            full = {u.name: u.full_name for u in frappe.get_all(
                "User", filters={"name": ["in", list(names)]},
                fields=["name", "full_name"])} if names else {}
            for r in rows:
                amt = float(r.amount or 0)
                if r.direction == "in":
                    cash_in += amt
                else:
                    cash_out += amt
                cash_moves.append({
                    "direction": r.direction, "amount": amt, "reason": r.reason or "",
                    "cashier": full.get(r.cashier) or r.cashier,
                    "time": frappe.utils.pretty_date(r.movement_time),
                })
    expected_drawer = float_amt + cash_sales + cash_in - cash_out

    return {
        "date": today,
        "revenue": revenue,
        "sales_count": sales_count,
        "avg_ticket": avg,
        "void_count": void_count,
        "void_amount": void_amt,
        "payments": payments,
        "hourly": hourly_list,
        "top_items": top_items,
        "cash": {
            "opened_at": opened_at,
            "cashier": cashier,
            "float": float_amt,
            "cash_sales": cash_sales,
            "cash_in": cash_in,
            "cash_out": cash_out,
            "expected": expected_drawer,
            "moves": cash_moves,
        },
        "approvals": _format_approvals(_pending_and_recent_approvals()),
    }
def _report_range(days=7, start=None, end=None, cap=92):
    """Resolve a report window.

    An explicit start+end — a chosen calendar month, say — wins and is capped to
    `cap` days; otherwise it is the last `days` ending today. Reversed dates are
    swapped rather than refused, because a date picker is how they get reversed
    and an empty report is a worse answer than the obvious one.

    Returns `(start_date, end_date, n_days)`.
    """
    from frappe.utils import add_days, date_diff, getdate, today as _today

    if start and end:
        s, e = getdate(start), getdate(end)
        if s > e:
            s, e = e, s
        if date_diff(e, s) + 1 > cap:
            s = add_days(e, -(cap - 1))
        return s, e, date_diff(e, s) + 1
    try:
        days = int(days)
    except Exception:
        days = 7
    days = max(1, min(days, cap))
    e = getdate(_today())
    return add_days(e, -(days - 1)), e, days


#: The order types the app writes, keyed by a normalised form of whatever is on
#: the row — the Select offers "Dine In\nTake Away\nDelivery", but a value typed
#: in the Desk, or written by an older build, need not match it exactly.
ORDER_TYPE_NAMES = {"dine in": "Dine In", "dinein": "Dine In",
                    "take away": "Take Away", "takeaway": "Take Away",
                    "delivery": "Delivery"}


@frappe.whitelist()
def get_manager_sales_report(days=7, start=None, end=None):
    """Daily sales trend + item-wise sales over a window (all cashiers).

    Powers the Manager app 'Reports' tab: a day-by-day revenue chart plus a
    full item-wise breakdown. The window is the last N days by default, or an
    explicit start/end — a whole calendar month picked in the UI.
    """
    roles.require_manager()
    from frappe.utils import getdate, add_days, today as _today, formatdate

    start, end, days = _report_range(days, start, end)
    # Raw SQL sees no permission layer, so the branch has to be said out loud.
    # Empty for an Owner, and on a site with one branch or no cost centres set.
    _inv_cond, _inv_params = branch.sql_cost_center()
    _pi_cond, _pi_params = branch.sql_cost_center("pi")

    # ── daily revenue + sale count (one grouped query) ──
    rows = frappe.db.sql(
        """
        SELECT posting_date AS d,
               COALESCE(SUM(grand_total), 0) AS revenue,
               COUNT(*) AS cnt
        FROM `tabPOS Invoice`
        WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s
        """ + _inv_cond + """
        GROUP BY posting_date
        """,
        (start, end) + _inv_params, as_dict=True)
    by_date = {str(r.d): {"revenue": float(r.revenue or 0), "count": int(r.cnt or 0)} for r in rows}

    daily = []
    cur = start
    while cur <= end:
        key = str(cur)
        rec = by_date.get(key, {"revenue": 0, "count": 0})
        daily.append({
            "date": key,
            "label": formatdate(cur, "d MMM"),
            "weekday": formatdate(cur, "EEE"),
            "revenue": rec["revenue"],
            "count": rec["count"],
        })
        cur = add_days(cur, 1)

    total_revenue = sum(d["revenue"] for d in daily)
    total_sales = sum(d["count"] for d in daily)
    avg_ticket = total_revenue / total_sales if total_sales else 0
    best_day = max(daily, key=lambda d: d["revenue"]) if daily else None

    # ── item-wise sales over the window (from paid POS Orders) ──
    item_rows = frappe.db.sql(
        """
        SELECT soi.item AS item,
               COALESCE(soi.item_name, soi.item) AS name,
               SUM(soi.qty) AS qty,
               SUM(soi.rate * soi.qty) AS revenue
        FROM `tabPOS Order Item` soi
        INNER JOIN `tabPOS Order` so ON so.name = soi.parent
        WHERE so.pos_invoice IS NOT NULL AND so.pos_invoice != ''
          AND so.creation >= %s AND so.creation < %s
        GROUP BY soi.item, name
        ORDER BY revenue DESC
        """,
        (start, add_days(end, 1)), as_dict=True)
    items = [{"item": r.item, "name": r.name, "qty": int(r.qty or 0),
              "revenue": float(r.revenue or 0)}
             for r in item_rows if r.name]
    items_revenue = sum(i["revenue"] for i in items) or 1

    # ── what did not sell ──────────────────────────────────────────────────
    # The query above can only see items that appeared on a bill, so an item
    # that sold nothing all month is absent from every list built from it —
    # and those are exactly the ones a manager is deciding whether to keep.
    # Read them off the active menu and carry them at zero.
    # Through `menu_items()` rather than a query of our own: it is the one place
    # that decides what "on the menu" means — an item is unavailable only when
    # the flag is explicitly 0, so a menu written before the flag existed still
    # counts. A ninth copy of that rule here is a menu that disagrees with the
    # till. It carries the item group too, from the same read.
    sold = {i["item"] for i in items if i.get("item")}
    unsold = [{"item": e["code"], "name": e["name"], "qty": 0, "revenue": 0.0,
               "rate": float(e.get("rate") or 0), "group": e.get("group") or "Other"}
              for e in menu.menu_items() if e.get("code") and e["code"] not in sold]

    # ── Food / Beverage, for the item card's filter ──
    # The sold list is built from bill lines, which carry no group; one read for
    # all of them. An item whose group was renamed or deleted reads as "Other"
    # rather than dropping out of the filter entirely.
    codes = [i["item"] for i in items if i.get("item")]
    groups = {}
    if codes:
        groups = {r.name: (r.item_group or "Other") for r in
                  frappe.get_all("Item", filters={"name": ["in", codes]},
                                 fields=["name", "item_group"])}
    for row in items:
        row["group"] = groups.get(row.get("item"), "Other")

    # ── revenue breakdown: service charge vs other charges vs net sales ──
    # Service charge is posted as a Sales Taxes and Charges row (description
    # "Service Charge", account "Service - LF") by _append_service_charge_tax,
    # NOT a line item. Bucket the range's tax rows by that description; net sales
    # is the remainder so the three segments always sum to Total revenue.
    tax = frappe.db.sql(
        """
        SELECT
          COALESCE(SUM(CASE WHEN t.description = 'Service Charge' THEN t.tax_amount ELSE 0 END), 0) AS sc,
          COALESCE(SUM(CASE WHEN t.description <> 'Service Charge' THEN t.tax_amount ELSE 0 END), 0) AS other
        FROM `tabSales Taxes and Charges` t
        INNER JOIN `tabPOS Invoice` pi ON pi.name = t.parent AND pi.docstatus = 1
        WHERE t.parenttype = 'POS Invoice' AND pi.posting_date BETWEEN %s AND %s
        """ + _pi_cond + """
        """,
        (start, end) + _pi_params, as_dict=True)[0]
    service_charge = float(tax.sc or 0)
    other_charges = float(tax.other or 0)
    net_sales = total_revenue - service_charge - other_charges

    # ── revenue by hour of day, summed across the window ──
    # The dashboard has this for today; over a month it stops being trivia and
    # becomes a rostering decision — which hours pay for the staff on them.
    hourly_rows = frappe.db.sql(
        """
        SELECT HOUR(pi.creation) AS h,
               COALESCE(SUM(pi.grand_total), 0) AS revenue,
               COUNT(*) AS cnt
        FROM `tabPOS Invoice` pi
        WHERE pi.docstatus = 1 AND pi.posting_date BETWEEN %s AND %s
        """ + _pi_cond + """
        GROUP BY h
        """,
        (start, end) + _pi_params, as_dict=True)
    by_hour = {int(r.h): r for r in hourly_rows}
    hourly = [{"hour": h,
               "revenue": float(by_hour[h].revenue or 0) if h in by_hour else 0.0,
               "count": int(by_hour[h].cnt or 0) if h in by_hour else 0}
              for h in range(24)]

    # ── dine in vs take away ──────────────────────────────────────────────
    # The order type lives on POS Order, the money on POS Invoice, so this is a
    # join rather than a sum of either alone. Revenue is the invoice's, not the
    # order's, so the split agrees with Total revenue and with every other card.
    #
    # Delivery is a valid order type nothing writes today: the till offers Dine
    # In and Take Away only, so an aggregator's order is rung up as Take Away.
    # It is carried here anyway so the day it is set the card already reads it.
    kind_rows = frappe.db.sql(
        """
        SELECT so.order_type AS kind, COUNT(*) AS cnt,
               COALESCE(SUM(pi.grand_total), 0) AS revenue
        FROM `tabPOS Invoice` pi
        INNER JOIN `tabPOS Order` so ON so.pos_invoice = pi.name
        WHERE pi.docstatus = 1 AND pi.posting_date BETWEEN %s AND %s
        """ + _pi_cond + """
        GROUP BY so.order_type
        """,
        (start, end) + _pi_params, as_dict=True)

    kinds, kind_counts = {}, {}
    for r in kind_rows:
        # "Dine-In" and "Dine In" are the same trade; a hyphen typed once into
        # the Desk must not open a second row on the chart.
        key = ORDER_TYPE_NAMES.get((r.kind or "").strip().lower().replace("-", " "), "Other")
        kinds[key] = kinds.get(key, 0) + float(r.revenue or 0)
        kind_counts[key] = kind_counts.get(key, 0) + int(r.cnt or 0)

    # A sale with no POS Order behind it is money this split cannot place.
    # Said out loud rather than dropped: a card that quietly adds up to less
    # than Total revenue is worse than one that admits the gap.
    stray = round(total_revenue - sum(kinds.values()), 2)
    if stray > 0.005:
        kinds["Not recorded"] = stray
        kind_counts["Not recorded"] = max(0, total_sales - sum(kind_counts.values()))
    order_types = [{"kind": k, "revenue": v, "count": kind_counts.get(k, 0)}
                   for k, v in sorted(kinds.items(), key=lambda x: -x[1]) if v]

    # ── how it was paid: one row per mode over the window ──
    # A different axis from revenue_breakdown above: that one says what the
    # money *was*, this says how it *arrived*. Both sum to total revenue, which
    # is why they are two cards and not six bars in one.
    #
    # A join rather than an `IN (…)` of the range's invoice names — a month of
    # trade is thousands of names to ship into the query, and the payment rows
    # are what is wanted anyway.
    pay = {}
    for r in frappe.db.sql(
        """
        SELECT sp.mode_of_payment AS mode, COALESCE(SUM(sp.amount), 0) AS amount
        FROM `tabSales Invoice Payment` sp
        INNER JOIN `tabPOS Invoice` pi ON pi.name = sp.parent AND pi.docstatus = 1
        WHERE sp.parenttype = 'POS Invoice' AND pi.posting_date BETWEEN %s AND %s
        """ + _pi_cond + """
        GROUP BY sp.mode_of_payment
        """,
        (start, end) + _pi_params, as_dict=True):
        # NULL and "" group separately in SQL and both read as "Other"
        mode = r.mode or "Other"
        pay[mode] = pay.get(mode, 0) + float(r.amount or 0)
    payments = [{"mode": m, "amount": a} for m, a in sorted(pay.items(), key=lambda x: -x[1]) if a]

    return {
        "days": days,
        "start": str(start),
        "end": str(end),
        "daily": daily,
        "items": items,
        "unsold": unsold,
        "items_revenue": items_revenue,
        "hourly": hourly,
        "total_revenue": total_revenue,
        "total_sales": total_sales,
        "avg_ticket": avg_ticket,
        "best_day": best_day,
        "payments": payments,
        "order_types": order_types,
        "revenue_breakdown": {
            "net_sales": net_sales,
            "service_charge": service_charge,
            "other_charges": other_charges,
            "total": total_revenue,
        },
    }
@frappe.whitelist()
def get_manager_day_report(date=None):
    """Full breakdown of a single day — powers the drill-down when a manager
    taps a bar on the Reports daily chart (payment mix, hourly, item-wise)."""
    roles.require_manager()
    from frappe.utils import getdate, add_days, formatdate, get_datetime, today as _today

    day = getdate(date or _today())
    day_start = str(day)
    day_end = str(add_days(day, 1))

    invoices = frappe.get_all("POS Invoice",
        filters={"docstatus": 1, "posting_date": day, **branch.invoice_filters()},
        fields=["name", "grand_total", "creation"])
    revenue = sum(float(i.grand_total or 0) for i in invoices)
    sales_count = len(invoices)
    avg = revenue / sales_count if sales_count else 0

    # payment mix + hourly revenue + the day's bill list (newest first).
    # One read of the payment rows for the whole day, not a document per bill:
    # this is the drill-down behind a bar on the manager's chart, so the busiest
    # day of the month is the one that used to cost the most to open.
    invoice_names = [i.name for i in invoices]
    rows_by_invoice = {}
    pay = {}
    if invoice_names:
        for r in frappe.get_all("Sales Invoice Payment",
                                filters={"parenttype": "POS Invoice",
                                         "parent": ["in", invoice_names]},
                                fields=["parent", "mode_of_payment", "amount"]):
            mode = r.mode_of_payment or "Other"
            amt = float(r.amount or 0)
            pay[mode] = pay.get(mode, 0) + amt
            if amt:
                rows_by_invoice.setdefault(r.parent, [])
                if mode not in rows_by_invoice[r.parent]:
                    rows_by_invoice[r.parent].append(mode)

    hourly = {}
    bills = []
    for inv in invoices:
        modes = rows_by_invoice.get(inv.name, [])
        created = get_datetime(inv.creation)
        h = created.hour
        hourly[h] = hourly.get(h, 0) + float(inv.grand_total or 0)
        bills.append({
            "name": inv.name,
            "no": inv.name.split("-")[-1],
            "time": created.strftime("%I:%M %p").lstrip("0"),
            "creation": str(inv.creation),
            "total": float(inv.grand_total or 0),
            "modes": modes,
        })
    bills.sort(key=lambda b: b["creation"], reverse=True)
    payments = [{"mode": m, "amount": a} for m, a in sorted(pay.items(), key=lambda x: -x[1])]
    hourly_list = [{"hour": h, "amount": hourly[h]} for h in sorted(hourly.keys())]

    # item-wise sales for the day (from paid POS Orders)
    item_rows = frappe.db.sql(
        """
        SELECT COALESCE(soi.item_name, soi.item) AS name,
               SUM(soi.qty) AS qty,
               SUM(soi.rate * soi.qty) AS revenue
        FROM `tabPOS Order Item` soi
        INNER JOIN `tabPOS Order` so ON so.name = soi.parent
        WHERE so.pos_invoice IS NOT NULL AND so.pos_invoice != ''
          AND so.creation >= %s AND so.creation < %s
        GROUP BY name
        ORDER BY revenue DESC
        """,
        (day_start, day_end), as_dict=True)
    items = [{"name": r.name, "qty": int(r.qty or 0), "revenue": float(r.revenue or 0)}
             for r in item_rows if r.name]

    # voids that day
    voids = frappe.get_list("POS Order",
        filters=[["docstatus", "=", 2], ["kitchen_status", "=", "Cancelled"],
                 ["modified", ">=", day_start], ["modified", "<", day_end]],
        fields=["grand_total"])

    return {
        "date": day_start,
        "label": formatdate(day, "EEE d MMM"),
        "revenue": revenue,
        "sales_count": sales_count,
        "avg_ticket": avg,
        "void_count": len(voids),
        "void_amount": sum(float(v.grand_total or 0) for v in voids),
        "payments": payments,
        "hourly": hourly_list,
        "items": items,
        "bills": bills,
    }
@frappe.whitelist()
def get_manager_monthly_sales(months=12):
    """Month-by-month revenue for the Manager Reports tab (last N calendar months,
    zero-filled so quiet months still show)."""
    roles.require_manager()
    import datetime
    from frappe.utils import getdate, formatdate, today as _today

    try:
        months = int(months)
    except Exception:
        months = 12
    months = max(1, min(months, 24))

    end = getdate(_today())
    start_year, start_month = end.year, end.month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = datetime.date(start_year, start_month, 1)

    _inv_cond, _inv_params = branch.sql_cost_center()
    rows = frappe.db.sql(
        """
        SELECT DATE_FORMAT(posting_date, '%%Y-%%m') AS ym,
               COALESCE(SUM(grand_total), 0) AS revenue,
               COUNT(*) AS cnt
        FROM `tabPOS Invoice`
        WHERE docstatus = 1 AND posting_date >= %s
        """ + _inv_cond + """
        GROUP BY ym
        """,
        (start,) + _inv_params, as_dict=True)
    by_ym = {r.ym: r for r in rows}

    out = []
    y, mo = start_year, start_month
    for _ in range(months):
        ym = "%04d-%02d" % (y, mo)
        r = by_ym.get(ym)
        revenue = float(r.revenue or 0) if r else 0
        cnt = int(r.cnt or 0) if r else 0
        out.append({"ym": ym, "label": formatdate(datetime.date(y, mo, 1), "MMM"),
                    "year": y, "yy": str(y)[2:], "revenue": revenue, "count": cnt,
                    "avg_ticket": (revenue / cnt) if cnt else 0})
        mo += 1
        if mo == 13:
            mo = 1
            y += 1

    best = max(out, key=lambda m: m["revenue"]) if out else None
    return {"months": out, "best": best,
            "total_revenue": sum(m["revenue"] for m in out),
            "total_sales": sum(m["count"] for m in out)}
@frappe.whitelist()
def get_manager_bill_detail(name=None):
    """Everything on one POS Invoice — powers the manager's tap-a-bill drill-down
    (items, discount, taxes/SC, payments, cashier, table)."""
    roles.require_manager()
    from frappe.utils import get_datetime, formatdate

    if not name or not frappe.db.exists("POS Invoice", name):
        frappe.throw(_("Bill not found"))
    doc = frappe.get_doc("POS Invoice", name)

    items = [{"name": i.item_name or i.item_code, "qty": float(i.qty or 0),
              "rate": float(i.rate or 0), "amount": float(i.amount or 0)}
             for i in (doc.items or [])]
    taxes = [{"label": (t.description or t.account_head or "Charge"),
              "amount": float(t.tax_amount or 0)}
             for t in (doc.taxes or []) if float(t.tax_amount or 0)]
    payments = [{"mode": p.mode_of_payment or "Other", "amount": float(p.amount or 0)}
                for p in (doc.payments or []) if float(p.amount or 0)]

    order = frappe.db.get_value("POS Order", {"pos_invoice": name},
        ["name", "restaurant_table", "order_source", "customer_name", "waiter_name"],
        as_dict=True) or {}
    table = (order.get("restaurant_table") or "").split("-")[-1] if order.get("restaurant_table") else ""

    created = get_datetime(doc.creation)
    return {
        "name": doc.name,
        "no": doc.name.split("-")[-1],
        "date_label": formatdate(doc.posting_date, "EEE d MMM"),
        "time": created.strftime("%I:%M %p").lstrip("0"),
        "cashier": frappe.get_value("User", doc.owner, "full_name") or doc.owner,
        "customer": order.get("customer_name") or doc.customer_name or "",
        "table": table,
        "source": order.get("order_source") or "",
        "items": items,
        "net_total": float(doc.net_total or 0),
        "discount": float(doc.discount_amount or 0),
        "taxes": taxes,
        "grand_total": float(doc.grand_total or 0),
        "payments": payments,
        "is_return": bool(doc.is_return),
    }
@frappe.whitelist()
def get_manager_purchase_report(days=7, start=None, end=None):
    """Daily + weekly purchasing spend over a window (approved bills only).

    Powers the Manager app 'Purchases' tab: a day-by-day and week-by-week spend
    chart plus top-vendor and top-item breakdowns. The window is the last N days
    by default, or an explicit start/end — a whole calendar month picked in the
    UI. Purchase Bill is non-submittable, so we filter on status='approved'
    (committed spend), not docstatus; the effective purchase date is
    `requested_at`.

    **Spend is the bill's `total`, not the sum of its lines.** A discount
    entered at approval is written to `total` and not to any line, so summing
    `qty * price` reports what was asked for rather than what was paid — and
    disagrees with Sales vs costs, which has always read `total`. Two screens,
    two answers, the difference being exactly the discounts.

    The item breakdown is the one place that cannot just read `total`: the
    discount belongs to the bill, not to any line on it. It is pro-rated down
    the lines by value, which is the only split that leaves the item list adding
    up to the same money as every other card here.
    """
    roles.require_manager()
    from frappe.utils import getdate, add_days, formatdate, today as _today

    start, end, days = _report_range(days, start, end)
    # requested_at is a Datetime; upper bound is exclusive start-of-next-day.
    range_start = str(start)
    range_end = str(add_days(end, 1))
    # Purchase Bill is ours, so it carries the branch itself — no cost centre
    # hop needed. Raw SQL sees no permission layer, so say it out loud.
    _pb_cond, _pb_params = branch.sql_restaurant("pb")
    # Revenue hangs off the invoice's cost centre, not the bill's restaurant —
    # a different hop, so a different condition.
    _inv_cond_pb, _inv_params_pb = branch.sql_cost_center("pi")

    # ── daily spend + bill count (one grouped query over the line items) ──
    rows = frappe.db.sql(
        """
        SELECT DATE(pb.requested_at) AS d,
               COALESCE(SUM(pb.total), 0) AS spend,
               COUNT(*) AS cnt
        FROM `tabPurchase Bill` pb
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY DATE(pb.requested_at)
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    by_date = {str(r.d): {"spend": float(r.spend or 0), "count": int(r.cnt or 0)} for r in rows}

    daily = []
    cur = start
    while cur <= end:
        key = str(cur)
        rec = by_date.get(key, {"spend": 0, "count": 0})
        daily.append({
            "date": key,
            "label": formatdate(cur, "d MMM"),
            "weekday": formatdate(cur, "EEE"),
            "spend": rec["spend"],
            "count": rec["count"],
        })
        cur = add_days(cur, 1)

    # ── weekly spend (ISO weeks, Monday-anchored) ──
    wrows = frappe.db.sql(
        """
        SELECT YEARWEEK(pb.requested_at, 3) AS yw,
               DATE(DATE_SUB(pb.requested_at, INTERVAL WEEKDAY(pb.requested_at) DAY)) AS wk_start,
               COALESCE(SUM(pb.total), 0) AS spend,
               COUNT(*) AS cnt
        FROM `tabPurchase Bill` pb
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY yw, wk_start
        ORDER BY wk_start
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    weekly = [{
        "week_start": str(r.wk_start),
        "label": formatdate(getdate(r.wk_start), "d MMM"),
        "weekday": "Wk",
        "spend": float(r.spend or 0),
        "count": int(r.cnt or 0),
    } for r in wrows]

    # ── top vendors ──
    vrows = frappe.db.sql(
        """
        SELECT pb.vendor AS name,
               COALESCE(SUM(pb.total), 0) AS spend,
               COUNT(*) AS cnt
        FROM `tabPurchase Bill` pb
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY pb.vendor
        ORDER BY spend DESC
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    vendors = [{"name": r.name or "—", "spend": float(r.spend or 0), "count": int(r.cnt or 0)}
               for r in vrows]

    # ── top items bought ──
    irows = frappe.db.sql(
        """
        SELECT COALESCE(pbi.item_name, pbi.item) AS name,
               SUM(pbi.qty) AS qty,
               COALESCE(SUM(pbi.qty * pbi.price * (
                   CASE WHEN sub.subtotal > 0 THEN pb.total / sub.subtotal ELSE 1 END
               )), 0) AS spend
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        INNER JOIN (
            SELECT parent, SUM(qty * price) AS subtotal
            FROM `tabPurchase Bill Item` GROUP BY parent
        ) sub ON sub.parent = pbi.parent
        WHERE pb.status = 'approved'
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY name
        ORDER BY spend DESC
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    items = [{"name": r.name, "qty": float(r.qty or 0), "spend": float(r.spend or 0)}
             for r in irows if r.name]

    # ── the same item, priced by different vendors ────────────────────────
    # The one report here that pays for itself. Grouped by UOM as well as item,
    # because a price per Kg and a price per Nos are not two quotes for the same
    # thing — comparing them would invent a spread that is not there.
    cmp_rows = frappe.db.sql(
        """
        SELECT COALESCE(pbi.item_name, pbi.item) AS name, pbi.uom AS uom,
               COUNT(DISTINCT pb.vendor) AS vendors,
               MIN(pbi.price) AS lo, MAX(pbi.price) AS hi,
               SUM(pbi.qty) AS qty
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved' AND pbi.price > 0
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY name, pbi.uom
        HAVING COUNT(DISTINCT pb.vendor) > 1 AND MAX(pbi.price) > MIN(pbi.price)
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    price_spread = []
    for r in cmp_rows:
        lo, hi, qty = float(r.lo or 0), float(r.hi or 0), float(r.qty or 0)
        if lo <= 0:
            continue
        price_spread.append({
            "name": r.name, "uom": r.uom or "", "vendors": int(r.vendors or 0),
            "low": lo, "high": hi, "spread_pct": round((hi - lo) * 100.0 / lo, 1),
            # What buying this period's quantity at the best price would have
            # saved. The honest ceiling, not a promise: the cheap vendor may not
            # carry the volume, and the dear one may be the only one on a Sunday.
            "could_save": round((hi - lo) * qty, 2),
        })
    price_spread.sort(key=lambda x: -x["could_save"])

    # ── what got dearer ───────────────────────────────────────────────────
    # old_price is written on every line and read by nothing. It is the buying
    # price the item carried before this bill, so the pair is a movement — and a
    # movement in what a kitchen buys weekly is a menu repricing decision.
    move_rows = frappe.db.sql(
        """
        SELECT COALESCE(pbi.item_name, pbi.item) AS name, pbi.uom AS uom,
               pbi.old_price AS was, pbi.price AS now_price, pb.requested_at AS at
        FROM `tabPurchase Bill Item` pbi
        INNER JOIN `tabPurchase Bill` pb ON pb.name = pbi.parent
        WHERE pb.status = 'approved' AND pbi.old_price > 0 AND pbi.price > 0
          AND pbi.price <> pbi.old_price
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        ORDER BY pb.requested_at ASC
        """,
        (range_start, range_end) + _pb_params, as_dict=True)
    # One row per item: the first price it carried in the window against the
    # last. Every wobble in between is noise a manager cannot act on.
    first_last = {}
    for r in move_rows:
        key = (r.name, r.uom or "")
        if key not in first_last:
            first_last[key] = [float(r.was or 0), float(r.now_price or 0)]
        else:
            first_last[key][1] = float(r.now_price or 0)
    price_moves = []
    for (name, uom), (was, now_price) in first_last.items():
        if was <= 0 or now_price == was:
            continue
        price_moves.append({"name": name, "uom": uom, "was": was, "now": now_price,
                            "change_pct": round((now_price - was) * 100.0 / was, 1)})
    price_moves.sort(key=lambda x: -abs(x["change_pct"]))

    # ── what is still owed, and for how long ──────────────────────────────
    # Deliberately NOT windowed: a bill from March is owed today whatever range
    # the screen is on. Ageing is from the request date, which is the closest
    # thing this app has to an invoice date.
    owed, owed_total = [], 0.0
    if frappe.db.has_column("Purchase Bill", "payment_status"):
        for r in frappe.db.sql(
            """
            SELECT pb.vendor AS name, COUNT(*) AS cnt,
                   COALESCE(SUM(pb.total), 0) AS amount,
                   MIN(pb.requested_at) AS oldest
            FROM `tabPurchase Bill` pb
            WHERE pb.status = 'approved'
              AND IFNULL(pb.payment_status, 'unpaid') <> 'paid'
            """ + _pb_cond + """
            GROUP BY pb.vendor
            ORDER BY amount DESC
            """, _pb_params, as_dict=True):
            age = (getdate(_today()) - getdate(r.oldest)).days if r.oldest else 0
            owed.append({"name": r.name or "—", "count": int(r.cnt or 0),
                         "amount": float(r.amount or 0), "oldest_days": max(0, age)})
        owed_total = sum(v["amount"] for v in owed)

    # ── committed but not yet approved, and what was refused ──────────────
    # The chart counts approved bills only, so a pending pile is spend nobody
    # can see coming and a rejected one is a decision nobody can review.
    waiting = {"pending": {"count": 0, "amount": 0.0},
               "rejected": {"count": 0, "amount": 0.0}}
    for r in frappe.db.sql(
        """
        SELECT pb.status AS status, COUNT(*) AS cnt, COALESCE(SUM(pb.total), 0) AS amount
        FROM `tabPurchase Bill` pb
        WHERE pb.status IN ('pending', 'rejected')
          AND pb.requested_at >= %s AND pb.requested_at < %s
        """ + _pb_cond + """
        GROUP BY pb.status
        """, (range_start, range_end) + _pb_params, as_dict=True):
        if r.status in waiting:
            waiting[r.status] = {"count": int(r.cnt or 0), "amount": float(r.amount or 0)}

    # ── how long a bill waits for a decision ──────────────────────────────
    turnaround = None
    if frappe.db.has_column("Purchase Bill", "resolved_at"):
        row = frappe.db.sql(
            """
            SELECT COUNT(*) AS cnt,
                   AVG(TIMESTAMPDIFF(HOUR, pb.requested_at, pb.resolved_at)) AS avg_h,
                   MAX(TIMESTAMPDIFF(HOUR, pb.requested_at, pb.resolved_at)) AS max_h
            FROM `tabPurchase Bill` pb
            WHERE pb.status <> 'pending' AND pb.resolved_at IS NOT NULL
              AND pb.resolved_at >= pb.requested_at
              AND pb.requested_at >= %s AND pb.requested_at < %s
            """ + _pb_cond + """
            """, (range_start, range_end) + _pb_params, as_dict=True)[0]
        if int(row.cnt or 0):
            turnaround = {"count": int(row.cnt or 0),
                          "avg_hours": round(float(row.avg_h or 0), 1),
                          "max_hours": round(float(row.max_h or 0), 1)}

    total_spend = sum(d["spend"] for d in daily)
    bill_count = sum(d["count"] for d in daily)
    avg_bill = total_spend / bill_count if bill_count else 0
    best_day = max(daily, key=lambda d: d["spend"]) if daily else None

    # ── spend against the revenue it bought ───────────────────────────────
    # Food cost % is the number a restaurant is actually run on, and the
    # Purchases tab showed spend with nothing to weigh it against.
    revenue = float(frappe.db.sql(
        """
        SELECT COALESCE(SUM(pi.grand_total), 0) FROM `tabPOS Invoice` pi
        WHERE pi.docstatus = 1 AND pi.posting_date BETWEEN %s AND %s
        """ + _inv_cond_pb, (start, end) + _inv_params_pb)[0][0] or 0)
    spend_pct = round(total_spend * 100.0 / revenue, 1) if revenue > 0 else 0

    return {
        "days": days,
        "price_spread": price_spread,
        "price_moves": price_moves,
        "owed": owed,
        "owed_total": owed_total,
        "waiting": waiting,
        "turnaround": turnaround,
        "revenue": revenue,
        "spend_pct": spend_pct,
        "start": str(start),
        "end": str(end),
        "daily": daily,
        "weekly": weekly,
        "vendors": vendors,
        "vendors_spend": sum(v["spend"] for v in vendors) or 1,
        "items": items,
        "items_spend": sum(i["spend"] for i in items) or 1,
        "total_spend": total_spend,
        "bill_count": bill_count,
        "avg_bill": avg_bill,
        "best_day": best_day,
    }
