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
from frappe.utils import today as _today

from luuvcrm import activity, branch, roles


def _branch_numbers(restaurant, date):
    """One branch's day. `None` for the money when it has no Cost Center — that
    is "cannot be attributed", which is a different thing from zero and must not
    be shown as a takings figure of nil."""
    centre = branch.cost_center(restaurant)
    out = {
        "restaurant": restaurant,
        "cost_center": centre,
        "revenue": None, "sales_count": None, "avg_ticket": None, "cash": None,
        "approvals_pending": frappe.db.count("POS Approval",
                                             {"status": "pending", branch.FIELD: restaurant}),
    }
    if not centre:
        return out

    invoices = frappe.get_all("POS Invoice",
                              filters={"docstatus": 1, "posting_date": date,
                                       "cost_center": centre},
                              fields=["name", "grand_total"])
    revenue = sum(float(i.grand_total or 0) for i in invoices)
    cash = 0.0
    for inv in invoices:
        for p in (frappe.get_doc("POS Invoice", inv.name).payments or []):
            if (p.mode_of_payment or "") == "Cash":
                cash += float(p.amount or 0)
    out.update({
        "revenue": revenue,
        "sales_count": len(invoices),
        "avg_ticket": revenue / len(invoices) if invoices else 0.0,
        "cash": cash,
    })
    return out


@frappe.whitelist()
def get_owner_overview(date=None):
    """Every branch's day, side by side, plus what is not set up yet."""
    roles.require_owner()
    date = date or _today()
    names = branch.all_restaurants()
    branches = [_branch_numbers(n, date) for n in names]

    def total(field):
        vals = [b[field] for b in branches if b[field] is not None]
        return sum(vals) if vals else 0

    return {
        "date": date,
        "branches": branches,
        "totals": {
            "revenue": total("revenue"),
            "sales_count": total("sales_count"),
            "cash": total("cash"),
            "approvals_pending": total("approvals_pending"),
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
    return {
        "viewing": branch.viewing(),
        "chosen": branch.selected(),
        "can_choose": branch.can_choose(),
        "restaurants": branch.available_restaurants(),
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
