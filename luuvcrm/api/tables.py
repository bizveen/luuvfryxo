"""The tables the restaurant seats people at — listing them, adding one, and
taking one out of service.

Until now a table could only be created in the Frappe Desk, which is a
developer's tool the owner is deliberately not given, and there was no way at
all to stop using one: `Restaurant Table` is hospitality's doctype and carries
no flag of its own. So a table that had gone — a corner turned into a service
station, a season's outdoor seating — stayed on the POS grid, on the waiter's
screen and on the QR sheet for ever.

**Deleting is not the answer.** Every bill the restaurant has ever raised links
to its table by name, and `POS Order.restaurant_table` is that link. Deleting a
table would either fail or orphan history. So `enabled` is a Check we add (see
`setup_doctype.CUSTOM_FIELDS`), default 1, and it works exactly the way the
menu's `available` does:

* the **pickers** hide a disabled table — the POS grid, the waiter's screen, the
  kiosk and online-order dropdowns, the offline payload, the QR sheet;
* the **bill already open on it is untouched**. It prices, prints, moves and
  pays. A table is turned off between services, not in the middle of one, and a
  flag that could strand a settled-up party is worse than no flag.

Everything here is branch-scoped. A manager sees and changes their own branch's
tables; an owner sees whichever branch they are working on. That is not a
courtesy — with two restaurants, an unscoped table list let a customer in one
branch put an order on a table standing in the other.
"""

import frappe
from frappe import _

from luuvcrm import branch, roles

#: The flag this module adds and every picker reads.
FIELD = "enabled"


def _has_flag():
    """Whether this site has been migrated yet.

    A missing column reads as "every table is enabled", which is the safe way
    round: the window between deploying the code and running the patch shows the
    full grid rather than an empty one, and a POS with no tables is a counter
    that cannot take an order.
    """
    return frappe.db.has_column("Restaurant Table", FIELD)


def _scope(restaurant=None):
    """Filters for one branch's tables, or none at all while there is one branch.

    `Restaurant Table.restaurant` is hospitality's own field and is *not*
    guaranteed to be filled on a site that has only ever had one outlet — the
    live restaurant's rows have it empty. Filtering on it there would hide every
    table, so the filter goes on only once there is a second restaurant to tell
    apart.
    """
    if len(branch.all_restaurants()) < 2:
        return {}
    here = restaurant or branch.viewing()
    return {"restaurant": here} if here else {}


def for_picker(restaurant=None):
    """The tables an ordering screen should offer: this branch's, in service.

    The one function every picker calls, so that "which tables are there" cannot
    mean two different things on two screens — which is exactly how the menu's
    availability loop ended up copied into eight files, five of them identical.
    """
    filters = _scope(restaurant)
    if _has_flag():
        filters[FIELD] = 1
    return frappe.get_all("Restaurant Table", filters=filters, fields=["name"],
                          order_by="name asc", limit_page_length=0)


def in_service(table, restaurant=None):
    """Whether a *new* order may be started on this table.

    Deliberately not asked about an order that already exists. `move_order` and
    every update path check that the table is real, not that it is in service:
    turning a table off must never strand a bill sitting on it.
    """
    if not table or not frappe.db.exists("Restaurant Table", table):
        return False
    if _has_flag() and not frappe.db.get_value("Restaurant Table", table, FIELD):
        return False
    scope = _scope(restaurant)
    if scope and frappe.db.get_value("Restaurant Table", table, "restaurant") != scope["restaurant"]:
        return False
    return True


@frappe.whitelist()
def get_tables_setup(restaurant=None):
    """Every table of the branch being worked on, in service or not.

    `open_orders` is here because it is the one thing somebody needs to know
    before turning a table off, and the screen cannot work it out for itself.
    Turning off a table with a bill on it is allowed — the bill is untouched —
    but the person doing it should be able to see that it will happen.
    """
    roles.require_manager()
    here = restaurant or branch.viewing()
    rows = []
    for t in frappe.get_all("Restaurant Table", filters=_scope(here),
                            fields=["name", "restaurant", "no_of_seats"],
                            order_by="name asc", limit_page_length=0):
        enabled = frappe.db.get_value("Restaurant Table", t.name, FIELD) if _has_flag() else 1
        rows.append({
            "name": t.name,
            "restaurant": t.restaurant or "",
            "seats": int(t.no_of_seats or 0),
            "enabled": int(enabled if enabled is not None else 1),
            "open_orders": frappe.db.count("POS Order", {"restaurant_table": t.name,
                                                         "docstatus": 0}),
        })
    return {
        "restaurant": here or "",
        "restaurants": branch.all_restaurants() if branch.can_choose() else [here] if here else [],
        "can_choose": branch.can_choose(),
        "can_toggle": _has_flag(),
        "tables": rows,
    }


@frappe.whitelist(methods=["POST"])
def add_table(seats=0, restaurant=None):
    """Add a table to the branch being worked on.

    **There is no name to give.** hospitality's `RestaurantTable.autoname` builds
    it from the restaurant — `New-Rest-04`, `Luuv-Fryxo-15` — and the screens
    show the last segment, which is the number the floor already calls it by. A
    name field here would have been ignored, so the screen does not offer one.

    That autoname also dereferences `self.restaurant` without checking it, so a
    branch is not optional however many restaurants there are: without one this
    raises inside hospitality with nothing a person could act on.
    """
    roles.require_manager()
    here = restaurant or branch.viewing()
    if not here:
        frappe.throw(_("Say which branch this table is in"))
    if not frappe.db.exists("Restaurant", here):
        frappe.throw(_("No such restaurant"))

    doc = frappe.get_doc({
        "doctype": "Restaurant Table",
        "restaurant": here,
        "no_of_seats": int(seats or 0),
        "minimum_seating": 0,
    })
    if _has_flag():
        doc.set(FIELD, 1)
    doc.insert(ignore_permissions=True, ignore_mandatory=True)
    frappe.db.commit()
    return {"ok": True, "table": doc.name, "restaurant": here}


@frappe.whitelist(methods=["POST"])
def set_table_enabled(table=None, enabled=1):
    """Take a table out of service, or put it back.

    Never deletes and never touches an order. The count of open bills is
    returned so the screen can say what is sitting on the table it has just
    turned off, rather than leaving somebody to find out at closing time.
    """
    roles.require_manager()
    if not table or not frappe.db.exists("Restaurant Table", table):
        frappe.throw(_("No such table"))
    if not _has_flag():
        frappe.throw(_("This site has no table enable/disable field yet — run bench migrate"))

    scope = _scope()
    if scope and frappe.db.get_value("Restaurant Table", table, "restaurant") != scope["restaurant"]:
        frappe.throw(_("That table is not in your branch"))

    flag = 1 if str(enabled) in ("1", "true", "True") else 0
    frappe.db.set_value("Restaurant Table", table, FIELD, flag)
    frappe.db.commit()
    return {"ok": True, "table": table, "enabled": flag,
            "open_orders": frappe.db.count("POS Order", {"restaurant_table": table,
                                                         "docstatus": 0})}
