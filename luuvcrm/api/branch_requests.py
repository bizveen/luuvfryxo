"""Inter-restaurant kitchen production requests.

One branch asks another branch's kitchen to prepare items (sauces, bases,
prepped food, and similar). This is deliberately a production/dispatch trail,
not a Stock Entry: receiving a request must never move warehouse stock without
an explicit inventory workflow.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from luuvcrm import branch, roles


ACTIVE_STATUSES = ("Requested", "Accepted", "Preparing", "Ready", "Dispatched")
TERMINAL_STATUSES = ("Received", "Rejected", "Cancelled")


def request_query_conditions(user=None):
    """Keep Desk and REST lists scoped just like the kitchen screen."""
    user = user or frappe.session.user
    if user == "Administrator":
        return ""
    active = branch.current(user)
    if not active or user == "Guest":
        return "1=0"
    value = frappe.db.escape(active)
    return (f"(`tabBranch Kitchen Request`.requesting_restaurant = {value} "
            f"OR `tabBranch Kitchen Request`.supplying_restaurant = {value})")


def request_has_permission(doc, user=None, permission_type=None):
    """Direct document access must not reveal another branch's requests."""
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    if user == "Guest" or permission_type not in (None, "read", "select"):
        return False
    active = branch.current(user)
    return bool(active and active in (doc.requesting_restaurant, doc.supplying_restaurant))

# (current status, requested status) -> which side may do it
TRANSITIONS = {
    ("Requested", "Accepted"): "supplier",
    ("Requested", "Rejected"): "supplier",
    ("Requested", "Cancelled"): "requester",
    ("Accepted", "Preparing"): "supplier",
    ("Accepted", "Rejected"): "supplier",
    ("Accepted", "Cancelled"): "requester",
    ("Preparing", "Ready"): "supplier",
    ("Ready", "Dispatched"): "supplier",
    ("Dispatched", "Received"): "requester",
}


def _require_access():
    roles.require_branch_requests()


def _active_restaurant():
    restaurant = branch.current()
    if not restaurant:
        frappe.throw(_("Select a restaurant from the Staff screen first"))
    return restaurant


def _json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        frappe.throw(_("Items must be a valid list"))
    if not isinstance(parsed, list):
        frappe.throw(_("Items must be a valid list"))
    return parsed


def _request_doc_for_active_branch(name):
    active = _active_restaurant()
    if not name or not frappe.db.exists("Branch Kitchen Request", name):
        frappe.throw(_("Kitchen request not found"))
    doc = frappe.get_doc("Branch Kitchen Request", name)
    if active not in (doc.requesting_restaurant, doc.supplying_restaurant):
        frappe.throw(_("You are not permitted to access this kitchen request"),
                     frappe.PermissionError)
    return doc, active


@frappe.whitelist()
def get_branch_kitchen_request_options():
    """Small lookup payload loaded only when the New Request sheet opens."""
    _require_access()
    active = _active_restaurant()
    restaurants = frappe.get_all(
        "Restaurant", filters={"name": ["!=", active]}, fields=["name"],
        order_by="name asc", limit_page_length=0)
    items = frappe.get_all(
        "Item", filters={"disabled": 0},
        fields=["name", "item_name", "stock_uom"],
        order_by="item_name asc, name asc", limit_page_length=0)
    return {
        "current_restaurant": active,
        "restaurants": restaurants,
        "items": items,
    }


@frappe.whitelist(methods=["POST"])
def create_branch_kitchen_request(supplying_restaurant=None, items=None,
                                  required_by=None, priority="Normal", notes=None):
    _require_access()
    active = _active_restaurant()
    supplying_restaurant = (supplying_restaurant or "").strip()
    if not supplying_restaurant or not frappe.db.exists("Restaurant", supplying_restaurant):
        frappe.throw(_("Choose a valid supplying restaurant"))
    if supplying_restaurant == active:
        frappe.throw(_("Choose another restaurant as the supplying kitchen"))

    clean_items = []
    for line in _json_list(items):
        item_code = (line.get("item") or "").strip() if isinstance(line, dict) else ""
        qty = flt(line.get("qty")) if isinstance(line, dict) else 0
        if not item_code or not frappe.db.exists("Item", item_code):
            frappe.throw(_("Choose a valid item on every line"))
        if qty <= 0:
            frappe.throw(_("Quantity must be greater than zero"))
        item = frappe.db.get_value(
            "Item", item_code, ["item_name", "stock_uom"], as_dict=True)
        clean_items.append({
            "item": item_code,
            "item_name": item.item_name or item_code,
            "qty": qty,
            "uom": (line.get("uom") or item.stock_uom or "").strip(),
            "notes": (line.get("notes") or "").strip()[:500],
        })
    if not clean_items:
        frappe.throw(_("Add at least one item"))

    doc = frappe.get_doc({
        "doctype": "Branch Kitchen Request",
        # The controller overwrites this from the authenticated session too.
        "requesting_restaurant": active,
        "supplying_restaurant": supplying_restaurant,
        "required_by": required_by or None,
        "priority": priority if priority in ("Normal", "Urgent") else "Normal",
        "notes": (notes or "").strip()[:1000],
        "items": clean_items,
    })
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def get_branch_kitchen_requests():
    """Incoming and outgoing requests for only the active restaurant."""
    _require_access()
    active = _active_restaurant()
    rows = frappe.get_all(
        "Branch Kitchen Request",
        or_filters=[
            ["Branch Kitchen Request", "requesting_restaurant", "=", active],
            ["Branch Kitchen Request", "supplying_restaurant", "=", active],
        ],
        fields=[
            "name", "requesting_restaurant", "supplying_restaurant", "status",
            "priority", "required_by", "notes", "requested_by", "requested_at",
            "last_updated_by", "last_updated_at", "creation", "modified",
        ],
        order_by="modified desc", limit_page_length=200,
    )
    names = [row.name for row in rows]
    item_map = {name: [] for name in names}
    history_map = {name: [] for name in names}
    if names:
        for row in frappe.get_all(
                "Branch Kitchen Request Item", filters={"parent": ["in", names]},
                fields=["parent", "item", "item_name", "qty", "uom", "notes", "idx"],
                order_by="parent asc, idx asc", limit_page_length=0):
            item_map[row.parent].append(row)
        for row in frappe.get_all(
                "Branch Kitchen Request Status", filters={"parent": ["in", names]},
                fields=["parent", "status", "changed_by", "changed_at", "note", "idx"],
                order_by="parent asc, idx asc", limit_page_length=0):
            history_map[row.parent].append(row)

    result = []
    for row in rows:
        row["direction"] = (
            "outgoing" if row.requesting_restaurant == active else "incoming")
        row["items"] = item_map[row.name]
        row["history"] = history_map[row.name]
        result.append(row)
    return {"current_restaurant": active, "requests": result}


@frappe.whitelist(methods=["POST"])
def update_branch_kitchen_request_status(request_name=None, status=None, note=None):
    _require_access()
    doc, active = _request_doc_for_active_branch(request_name)
    target = (status or "").strip()
    side = TRANSITIONS.get((doc.status, target))
    if not side:
        frappe.throw(_("{0} cannot move to {1}").format(doc.status, target or _("blank")))

    actor_side = "requester" if active == doc.requesting_restaurant else "supplier"
    if actor_side != side:
        frappe.throw(_("Only the {0} branch can make this status change").format(side),
                     frappe.PermissionError)

    changed_at = now_datetime()
    doc.status = target
    doc.last_updated_by = frappe.session.user
    doc.last_updated_at = changed_at
    doc.append("status_history", {
        "status": target,
        "changed_by": frappe.session.user,
        "changed_at": changed_at,
        "note": (note or "").strip()[:500],
    })
    doc.flags.ignore_permissions = True
    doc.save()
    return {"name": doc.name, "status": doc.status}
