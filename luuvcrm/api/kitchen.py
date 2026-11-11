"""The kitchen display and the statuses it moves an order through.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime

from luuvcrm import branch, roles
from luuvcrm.api.orders import SCREEN_ORDER_CEILING, _is_cus_call_pending, lines_by_order
from luuvcrm.api.shifts import _get_invoice_doc

#: How far back the kitchen queue reaches.
#:
#: The filter used to be status alone, with no date — and the comment beside it
#: said so: "an order that never reaches Served is on this screen for ever".
#: What that missed is the *ceiling*. The list is ordered oldest-first and cut
#: at SCREEN_ORDER_CEILING, so once more tickets than that have piled up
#: unserved, the screen fills with ancient ones and **every new order becomes
#: invisible to the kitchen** — with nothing on the screen saying so. Found on
#: the test site at 275 pending against a ceiling of 200.
#:
#: Older versions did not close a ticket after full payment, so over months the
#: ones nobody explicitly served accumulated. The customer flow now completes
#: an existing ticket at payment; this window remains a safety net for legacy
#: and otherwise abandoned tickets.
#:
#: Two days rather than "today": a shift that starts at 6pm and ends at 2am
#: would otherwise lose half its tickets at midnight. Anything older than this
#: is not work the kitchen is about to do — but it is still *counted* and said
#: out loud below, because the whole failure here was silence.
KITCHEN_WINDOW_HOURS = 48


# ─── Kitchen Display ─────────────────────────────────────
@frappe.whitelist()
def get_kitchen_orders():
    from frappe.utils import add_to_date

    _fields = ["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
               "grand_total", "creation", "order_source", "notes", "kitchen_status"]
    if frappe.db.has_column("POS Order", "scheduled_time"):
        _fields.append("scheduled_time")

    waiting = {"kitchen_status": ["not in", ["Served", "Draft", "Cancelled"]],
               branch.FIELD: branch.viewing()}
    cutoff = add_to_date(now_datetime(), hours=-KITCHEN_WINDOW_HOURS)

    # Counted through get_list, not db.count: the kitchen on a two-branch site
    # is pinned by a User Permission, and a count that ignores it would report
    # tickets this screen is not allowed to show.
    in_window = {**waiting, "creation": [">=", cutoff]}
    waiting_now = len(frappe.get_list("POS Order", filters=in_window, fields=["name"],
                                      limit_page_length=0))
    total_open = len(frappe.get_list("POS Order", filters=waiting, fields=["name"],
                                     limit_page_length=0))

    orders = frappe.get_list("POS Order",
        filters=in_window,
        fields=_fields,
        order_by="creation asc",
        limit_page_length=SCREEN_ORDER_CEILING,
    )
    now_dt = now_datetime()
    lines = lines_by_order([o.name for o in orders])
    groups = _kitchen_groups({i.item for rows in lines.values() for i in rows})
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        elapsed = (now_dt - o["creation"]).total_seconds()
        o["minutes"] = max(0, int(elapsed / 60))
        o["seconds"] = max(0, int(elapsed))
        items = []
        for i in lines.get(o.name, []):
            meta = groups.get(i.item) or {}
            items.append({
                "item": i.item, "item_name": i.item_name or i.item,
                "qty": i.qty, "rate": i.rate,
                "group": meta.get("item_group") or "",
                "kitchen_group": meta.get("kitchen_group_name") or "",
                "notes": i.get("notes") or ""
            })
        o["items_json"] = json.dumps(items)

    return {
        "orders": orders,
        # Everything the screen is not showing, and why. A kitchen display that
        # quietly drops the ticket somebody just fired is the failure this
        # replaces; a line saying "200 of 275" is not.
        "shown": len(orders),
        "waiting": waiting_now,
        "truncated": max(waiting_now - len(orders), 0),
        "stale": max(total_open - waiting_now, 0),
        "window_hours": KITCHEN_WINDOW_HOURS,
    }


def _kitchen_groups(item_codes):
    """Which section of the kitchen each item belongs to, in two reads.

    This was a full `Item` document per line plus a Kitchen Group lookup per
    line — twenty open orders of four lines each is eighty documents, every ten
    seconds, on the screen the kitchen never closes.
    """
    if not item_codes:
        return {}
    fields = ["name", "item_group"]
    has_kg = frappe.db.has_column("Item", "kitchen_group")
    if has_kg:
        fields.append("kitchen_group")
    rows = frappe.get_all("Item", filters={"name": ["in", list(item_codes)]},
                          fields=fields, limit_page_length=0)
    names = {}
    if has_kg:
        wanted = {r.kitchen_group for r in rows if r.get("kitchen_group")}
        if wanted:
            names = {g.name: g.group_name for g in frappe.get_all(
                "Kitchen Group", filters={"name": ["in", list(wanted)]},
                fields=["name", "group_name"], limit_page_length=0)}
    return {r.name: {"item_group": r.item_group or "",
                     "kitchen_group_name": names.get(r.get("kitchen_group"), "")}
            for r in rows}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_processing():
    roles.require_kitchen()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    branch.require_access("POS Order", order_name)
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Processing")
    return {"status": "processing", "name": order_name}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_ready():
    roles.require_kitchen()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    branch.require_access("POS Order", order_name)
    order = frappe.get_doc("POS Order", order_name, for_update=True)
    if _is_cus_call_pending(order):
        frappe.throw(_("Confirm the APP order before marking it ready."))
    channel = str(order.get("waiter_name") or "").strip().upper()
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Ready")
    # Delivery is asynchronous and starts only after this transaction commits;
    # the kitchen button must never wait on Firebase or fail because it is down.
    push_queued = False
    if channel == "CUS":
        from luuvcrm.api.push import queue_order_ready_push
        push_queued = queue_order_ready_push(order_name)
    return {"status": "ready", "name": order_name, "push_queued": push_queued}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_served():
    roles.require_kitchen()
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))

    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order already served"))

    if pos_order.pos_invoice:
        try:
            inv = _get_invoice_doc(pos_order.pos_invoice)
            if inv.docstatus == 0:
                inv.flags.ignore_permissions = True
                inv.submit()
        except Exception:
            pass

    pos_order.db_set("docstatus", 1)
    pos_order.db_set("kitchen_status", "Served")
    return {"status": "served", "name": order_name}
# ─── Kitchen Group Setup ──────────────────────────────────

@frappe.whitelist()
def setup_kitchen_groups():
    """Create Kitchen Group DocType + assign items to groups."""
    frappe.only_for("System Manager")
    created = []

    if not frappe.db.exists("DocType", "Kitchen Group"):
        doc = frappe.get_doc({
            "doctype": "DocType", "name": "Kitchen Group", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "group_name", "label": "Group Name", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "display_order", "label": "Display Order", "fieldtype": "Int", "default": 0},
                {"fieldname": "color", "label": "Color", "fieldtype": "Data", "description": "Hex color e.g. #F59E0B"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        })
        doc.insert()
        created.append("Kitchen Group")

        # Create default groups
        default_groups = [
            ("Kottu", 1, "#F59E0B"),
            ("Rice & Noodles", 2, "#3B82F6"),
            ("Curries & Gravies", 3, "#10B981"),
            ("Biryani", 4, "#8B5CF6"),
            ("Pizza", 5, "#EC4899"),
            ("Beverages", 6, "#06B6D4"),
            ("Coffee & Tea", 7, "#F97316"),
            ("Desserts", 8, "#14B8A6"),
            ("Sides & Starters", 9, "#6366F1"),
            ("Soups & Salads", 10, "#84CC16"),
            ("Grills & Sizzlers", 11, "#EF4444"),
            ("Breads", 12, "#D946EF"),
            ("Other", 99, "#6B7280"),
        ]
        for name, order, color in default_groups:
            g = frappe.get_doc({
                "doctype": "Kitchen Group",
                "group_name": name,
                "display_order": order,
                "color": color,
            })
            g.flags.ignore_permissions = True
            g.insert()
        created.append(f"{len(default_groups)} default groups")

    # Add kitchen_group field to Item
    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "kitchen_group"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item",
            "fieldname": "kitchen_group",
            "label": "Kitchen Group",
            "fieldtype": "Link",
            "options": "Kitchen Group",
            "insert_after": "item_group",
        })
        cf.insert()
        created.append("kitchen_group field on Item")

    return {"status": "created" if created else "already_exists", "created": created}
@frappe.whitelist()
def assign_kitchen_groups():
    """Assign items to kitchen groups based on their item_group."""
    mapping = {
        "Kottu": "Kottu", "Cheese Kottu": "Kottu", "Signature Kottu": "Kottu",
        "Fried Rice": "Rice & Noodles", "Rice & Curry": "Rice & Noodles",
        "Biryani & Naan Combos": "Biryani", "Biryani": "Biryani",
        "Pizza": "Pizza",
        "Grills": "Grills & Sizzlers", "Sizzler Platters": "Grills & Sizzlers",
        "North Asian": "Rice & Noodles", "Sri Lankan Fusion": "Rice & Noodles",
        "Indian Cuisine": "Curries & Gravies",
        "European Dishes & Salads": "Sides & Starters",
        "Soups": "Soups & Salads",
        "Sides & Accompaniments": "Sides & Starters",
        "Vegetarian": "Curries & Gravies",
        "Coffee & Tea": "Coffee & Tea",
        "Beverages": "Beverages",
        "Desserts": "Desserts",
    }
    items = frappe.get_all("Item", fields=["name", "item_group"])
    assigned = 0
    for item in items:
        grp = item.item_group or ""
        kg_name = mapping.get(grp, "Other")
        kg = frappe.db.get_value("Kitchen Group", {"group_name": kg_name}, "name")
        if kg:
            frappe.db.set_value("Item", item.name, "kitchen_group", kg)
            assigned += 1
    frappe.db.commit()
    return {"status": "done", "assigned": assigned}
