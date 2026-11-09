"""The Owner's screen: two restaurants side by side, and the setup behind them.

Phase 2c built the machinery for more than one restaurant — a `restaurant` link
on every row, a User Permission per manager, `get_list` applying it — and then
left it usable only from a Python console. Creating the second branch, giving it
a Cost Center, pointing a till at it and restricting a manager to it were all
things a developer had to do. `roles.require_owner()` existed and nothing called
it. This is the screen that makes the role mean something.

Two things it does:

**Compares.** An Owner has no User Permission, so every report already shows
them both branches — *summed*. One number for two restaurants answers the wrong
question. Here each branch stands in its own column.

**Sets up.** The four things that have to be true before two branches stay
apart, each of which is otherwise a thing somebody does once in the Desk,
forgets, and cannot tell from working: a Cost Center per branch, a Warehouse per
branch, a till that names its branch, and a manager restricted to theirs.
`branch.check()` is the same list, and it is shown here rather than hidden in a
console command.

The numbers are the manager dashboard's, deliberately: revenue is the sum of
submitted POS Invoice grand totals for the day, cash is the Cash rows on their
payments. Two definitions of "today's revenue" would be worse than none.
"""

import frappe
from frappe.utils import add_days, date_diff, formatdate, getdate, today as _today

from luuvcrm import activity, branch, roles


def _change(current, previous):
    """Percentage change, with a new business period represented explicitly."""
    if not previous:
        return 0.0 if not current else None
    return round((current - previous) * 100 / previous, 1)


def _overview_range(days=7, start=None, end=None, date=None):
    """Resolve the owner's requested period, capped at one calendar year."""
    if date and not (start or end):
        start = end = date
    if start and end:
        start, end = getdate(start), getdate(end)
        if start > end:
            start, end = end, start
        if date_diff(end, start) + 1 > 366:
            start = add_days(end, -365)
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 366))
        end = getdate(_today())
        start = add_days(end, -(days - 1))
    return start, end, date_diff(end, start) + 1


def _branch_numbers(restaurant, start, end, previous_start=None, previous_end=None):
    """One branch's period. `None` for the money when it has no Cost Center — that
    is "cannot be attributed", which is a different thing from zero and must not
    be shown as a takings figure of nil."""
    centre = branch.cost_center(restaurant)
    details = frappe.db.get_value("Restaurant", restaurant, ["warehouse", "company", "city"],
                                  as_dict=True) or frappe._dict()
    out = {
        "restaurant": restaurant,
        "city": details.get("city") or "",
        "cost_center": centre,
        "warehouse": details.get("warehouse"),
        "company": details.get("company"),
        "revenue": None, "sales_count": None, "avg_ticket": None, "cash": None,
        "previous_revenue": None, "revenue_change": None, "share": 0,
        "daily": {}, "expenses": 0.0, "expense_daily": {},
        "approvals_pending": frappe.db.count("POS Approval",
                                             {"status": "pending", branch.FIELD: restaurant}),
    }

    # Approved supplier bills and standalone expenses are both money leaving
    # the branch. Keep the header total for bills so whole-bill discounts are
    # represented exactly once. Unlike revenue, both doctypes name their
    # restaurant directly, so spend can still be attributed while a new branch
    # is waiting for its Cost Center.
    spend_by_day = {}
    if frappe.db.exists("DocType", "Purchase Bill"):
        for row in frappe.db.sql("""
                SELECT purchase_date AS day, COALESCE(SUM(total), 0) AS amount
                FROM `tabPurchase Bill`
                WHERE status = 'approved' AND purchase_date BETWEEN %s AND %s
                  AND restaurant = %s
                GROUP BY purchase_date
                """, (start, end, restaurant), as_dict=True):
            spend_by_day[str(row.day)] = spend_by_day.get(str(row.day), 0) + float(row.amount or 0)
    if frappe.db.exists("DocType", "Expense"):
        for row in frappe.db.sql("""
                SELECT COALESCE(expense_date, DATE(creation)) AS day,
                       COALESCE(SUM(amount), 0) AS amount
                FROM `tabExpense`
                WHERE status = 'approved'
                  AND COALESCE(expense_date, DATE(creation)) BETWEEN %s AND %s
                  AND restaurant = %s
                GROUP BY day
                """, (start, end, restaurant), as_dict=True):
            spend_by_day[str(row.day)] = spend_by_day.get(str(row.day), 0) + float(row.amount or 0)
    out["expense_daily"] = spend_by_day
    out["expenses"] = sum(spend_by_day.values())
    if not centre:
        return out

    rows = frappe.db.sql("""
        SELECT posting_date AS day, COUNT(*) AS sales_count,
               COALESCE(SUM(grand_total), 0) AS revenue
        FROM `tabPOS Invoice`
        WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s AND cost_center = %s
        GROUP BY posting_date ORDER BY posting_date
    """, (start, end, centre), as_dict=True)
    revenue = sum(float(r.revenue or 0) for r in rows)
    sales_count = sum(int(r.sales_count or 0) for r in rows)
    cash = float(frappe.db.sql("""
        SELECT COALESCE(SUM(p.amount), 0)
        FROM `tabSales Invoice Payment` p
        INNER JOIN `tabPOS Invoice` pi ON pi.name = p.parent AND pi.docstatus = 1
        WHERE p.parenttype = 'POS Invoice' AND p.mode_of_payment = 'Cash'
          AND pi.posting_date BETWEEN %s AND %s AND pi.cost_center = %s
    """, (start, end, centre))[0][0] or 0)
    previous_revenue = 0.0
    if previous_start is not None and previous_end is not None:
        previous_revenue = float(frappe.db.sql("""
            SELECT COALESCE(SUM(grand_total), 0) FROM `tabPOS Invoice`
            WHERE docstatus = 1 AND posting_date BETWEEN %s AND %s AND cost_center = %s
        """, (previous_start, previous_end, centre))[0][0] or 0)
    out.update({
        "revenue": revenue,
        "sales_count": sales_count,
        "avg_ticket": revenue / sales_count if sales_count else 0.0,
        "cash": cash,
        "previous_revenue": previous_revenue,
        "revenue_change": _change(revenue, previous_revenue),
        "daily": {str(r.day): float(r.revenue or 0) for r in rows},
    })
    return out


@frappe.whitelist()
def get_owner_overview(date=None, days=7, start=None, end=None):
    """Decision dashboard across restaurants for a selected period.

    ``date`` remains supported for older clients and means a one-day range.
    """
    roles.require_owner()
    start, end, days = _overview_range(days=days, start=start, end=end, date=date)
    previous_end = add_days(start, -1)
    previous_start = add_days(previous_end, -(days - 1))
    names = branch.all_restaurants()
    branches = [_branch_numbers(n, start, end, previous_start, previous_end) for n in names]

    def total(field):
        vals = [b[field] for b in branches if b[field] is not None]
        return sum(vals) if vals else 0

    revenue = total("revenue")
    sales_count = total("sales_count")
    cash = total("cash")
    previous_revenue = total("previous_revenue")
    expenses = total("expenses")
    for row in branches:
        row["share"] = round((row["revenue"] or 0) * 100 / revenue, 1) if revenue else 0

    daily = []
    cursor = start
    while cursor <= end:
        key = str(cursor)
        by_branch = {row["restaurant"]: float(row["daily"].get(key, 0))
                     for row in branches if row["revenue"] is not None}
        expense_by_branch = {row["restaurant"]: float(row["expense_daily"].get(key, 0))
                             for row in branches}
        daily.append({
            "date": key,
            "label": formatdate(cursor, "d MMM"),
            "weekday": formatdate(cursor, "EEE"),
            "revenue": sum(by_branch.values()),
            "branches": by_branch,
            "expenses": sum(expense_by_branch.values()),
            "expense_branches": expense_by_branch,
        })
        cursor = add_days(cursor, 1)

    ranked = sorted((b for b in branches if b["revenue"] is not None),
                    key=lambda b: b["revenue"], reverse=True)
    best_day = max(daily, key=lambda d: d["revenue"]) if daily else None
    range_label = (formatdate(start, "d MMM yyyy") if days == 1 else
                   f"{formatdate(start, 'd MMM')} – {formatdate(end, 'd MMM yyyy')}")

    return {
        "date": str(end),
        "start": str(start),
        "end": str(end),
        "days": days,
        "range_label": range_label,
        "previous_start": str(previous_start),
        "previous_end": str(previous_end),
        "branches": branches,
        "daily": daily,
        "totals": {
            "revenue": revenue,
            "sales_count": sales_count,
            "avg_ticket": revenue / sales_count if sales_count else 0,
            "cash": cash,
            "expenses": expenses,
            "cash_share": round(cash * 100 / revenue, 1) if revenue else 0,
            "approvals_pending": total("approvals_pending"),
            "previous_revenue": previous_revenue,
            "revenue_change": _change(revenue, previous_revenue),
        },
        "leaders": {
            "best_branch": ranked[0]["restaurant"] if ranked and ranked[0]["revenue"] else "",
            "best_branch_revenue": ranked[0]["revenue"] if ranked else 0,
            "best_day": best_day,
        },
        "problems": branch.check(),
        "single_branch": len(names) < 2,
    }


@frappe.whitelist()
def get_branch_setup():
    """What each branch is wired to, and what it is missing.

    Everything here is configuration somebody does once and then cannot tell
    from working — which is exactly why it needs a screen that shows it.
    """
    roles.require_owner()
    rows = []
    for name in branch.all_restaurants():
        row = frappe.db.get_value("Restaurant", name,
                                  ["company", "cost_center", "warehouse"],
                                  as_dict=True) or frappe._dict()
        tills = frappe.get_all("POS Profile", filters={branch.FIELD: name}, pluck="name") \
            if frappe.db.has_column("POS Profile", branch.FIELD) else []
        managers = frappe.get_all("User Permission",
                                  filters={"allow": "Restaurant", "for_value": name},
                                  pluck="user")
        rows.append({
            "restaurant": name,
            "company": row.get("company"),
            "cost_center": row.get("cost_center"),
            "warehouse": row.get("warehouse"),
            "tills": tills,
            "managers": managers,
            "rows": frappe.db.count("POS Order", {branch.FIELD: name}),
        })
    return {
        "branches": rows,
        "companies": frappe.get_all("Company", pluck="name"),
        "cost_centers": frappe.get_all("Cost Center", filters={"is_group": 0}, pluck="name"),
        "warehouses": frappe.get_all("Warehouse", filters={"is_group": 0}, pluck="name"),
        "tills": frappe.get_all("POS Profile", pluck="name"),
        "managers": _assignable_managers(),
        "problems": branch.check(),
    }


def _assignable_managers():
    """Users holding Restaurant Manager. An Owner is deliberately not here: the
    absence of a User Permission is what makes them an Owner, so restricting one
    to a branch would quietly demote them."""
    out = []
    for user in frappe.get_all("User", filters={"enabled": 1}, pluck="name"):
        if user in ("Administrator", "Guest"):
            continue
        held = set(frappe.get_roles(user))
        if roles.RESTAURANT_MANAGER in held and not (held & set(roles.OWNER_ROLES)):
            out.append(user)
    return out


@frappe.whitelist(methods=["POST"])
def create_branch(restaurant_name=None, company=None, cost_center=None, warehouse=None):
    """Add a restaurant.

    `Restaurant` is hospitality's doctype and autonames by prompt, so the name
    is given rather than derived. company / default_customer / invoice prefix are
    required by it; the first two are copied from an existing branch when they
    are not supplied, because a second outlet of the same business shares them.
    """
    roles.require_owner()
    name = (restaurant_name or "").strip()
    if not name:
        frappe.throw("A name is required")
    if frappe.db.exists("Restaurant", name):
        frappe.throw(f"{name} already exists")

    existing = frappe.db.get_value("Restaurant", {}, ["company", "default_customer",
                                                      "active_menu", "service_charge"],
                                   as_dict=True) or frappe._dict()
    doc = frappe.get_doc({
        "doctype": "Restaurant",
        "__newname": name,
        "company": company or existing.company,
        "default_customer": existing.default_customer,
        "invoice_series_prefix": "".join(c for c in name.upper() if c.isalnum())[:4] or "REST",
        "active_menu": existing.active_menu,
        "service_charge": existing.service_charge,
        "cost_center": cost_center,
        "warehouse": warehouse,
    })
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    frappe.db.commit()
    return {"ok": True, "restaurant": doc.name, "problems": branch.check()}


@frappe.whitelist(methods=["POST"])
def update_branch(restaurant=None, cost_center=None, warehouse=None):
    """Give a branch its accounting dimensions — the two things that actually
    keep one restaurant's money apart from the other's."""
    roles.require_owner()
    if not restaurant or not frappe.db.exists("Restaurant", restaurant):
        frappe.throw("No such restaurant")
    if cost_center and frappe.db.get_value("Cost Center", cost_center, "is_group"):
        frappe.throw("A group Cost Center cannot be posted to — pick a leaf")
    updates = {}
    if cost_center is not None:
        updates["cost_center"] = cost_center or None
    if warehouse is not None:
        updates["warehouse"] = warehouse or None
    if updates:
        frappe.db.set_value("Restaurant", restaurant, updates)
        frappe.db.commit()
    return {"ok": True, "problems": branch.check()}


@frappe.whitelist()
def get_branch_choice():
    """Which branch this person is looking at, and whether they may change it.

    Single-branch staff are pinned and get no picker. Owners, Administrator and
    staff explicitly permitted for several branches can switch among the
    restaurants they are allowed to operate.
    """
    if not roles.has_any(roles.KITCHEN_ROLES | roles.PURCHASE_ROLES):
        frappe.throw("You are not permitted to use an operational app",
                     frappe.PermissionError)
    restaurants = branch.available_restaurants()
    viewing = branch.viewing()
    return {
        "viewing": viewing,
        "viewing_label": branch.display_label(viewing),
        "chosen": branch.selected(),
        "can_choose": branch.can_choose(),
        # Keep record IDs as the selection values; labels are presentation only.
        # A branch such as `Luuv Fryxo` may be branded `Rangiri Luuv` and located
        # in Dambulla, so the operator sees `Rangiri Luuv — Dambulla` without
        # changing what gets written to links and user defaults.
        "restaurants": restaurants,
        "restaurant_labels": {name: branch.display_label(name) for name in restaurants},
    }


@frappe.whitelist(methods=["POST"])
def set_branch_choice(restaurant=None):
    """Choose the branch to look at, or clear it with a blank.

    This is the shared operational choice: it scopes the five staff apps and
    their reports, and `branch.current()` stamps new rows with the same branch.
    """
    if not roles.has_any(roles.KITCHEN_ROLES | roles.PURCHASE_ROLES):
        frappe.throw("You are not permitted to use an operational app",
                     frappe.PermissionError)
    chosen = branch.select(restaurant)

    activity.log_activity(
        "branch.view", label="Now looking at %s" % (chosen or "the first branch"),
        ref_doctype="Restaurant", ref_name=chosen or None,
        detail={"chosen": chosen})
    return {"ok": True, "chosen": chosen, "viewing": branch.viewing()}


@frappe.whitelist(methods=["POST"])
def assign_till(pos_profile=None, restaurant=None):
    """Point a till at a branch. This is the hop `branch.current()` relies on:
    with one Company shared across branches, `POS Profile.company` identifies
    nothing, so the profile has to name its restaurant."""
    roles.require_owner()
    if not frappe.db.exists("POS Profile", pos_profile):
        frappe.throw("No such POS Profile")
    if restaurant and not frappe.db.exists("Restaurant", restaurant):
        frappe.throw("No such restaurant")
    frappe.db.set_value("POS Profile", pos_profile, branch.FIELD, restaurant or None)
    frappe.db.commit()
    return {"ok": True, "problems": branch.check()}


@frappe.whitelist(methods=["POST"])
def assign_manager(user=None, restaurant=None):
    """Restrict a manager to one branch, or lift the restriction.

    A User Permission is the whole mechanism — Frappe applies it to every
    doctype linked to `Restaurant` on every `get_list`. Passing no restaurant
    removes every restriction the user has, which makes them see all branches.
    """
    roles.require_owner()
    if not user or not frappe.db.exists("User", user):
        frappe.throw("No such user")
    if restaurant:
        branch.unassign(user)
        branch.assign(user, restaurant)
    else:
        branch.unassign(user)
    return {"ok": True, "assigned": branch.assigned(user), "problems": branch.check()}
