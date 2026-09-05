"""The kitchen display and the statuses it moves an order through.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime

from luuvcrm.api.shifts import _get_invoice_doc


# ─── Kitchen Display ─────────────────────────────────────
@frappe.whitelist()
def get_kitchen_orders():
    _fields = ["name", "customer_name", "waiter_name", "mobile", "restaurant_table",
               "grand_total", "creation", "order_source", "notes", "kitchen_status"]
    if frappe.db.has_column("POS Order", "scheduled_time"):
        _fields.append("scheduled_time")
    orders = frappe.get_list("POS Order",
        filters={"kitchen_status": ["not in", ["Served", "Draft", "Cancelled"]]},
        fields=_fields,
        order_by="creation asc"
    )
    now_dt = now_datetime()
    for o in orders:
        o["time_ago"] = frappe.utils.pretty_date(o["creation"])
        elapsed = (now_dt - o["creation"]).total_seconds()
        o["minutes"] = max(0, int(elapsed / 60))
        o["seconds"] = max(0, int(elapsed))
        doc = frappe.get_doc("POS Order", o.name)
        items = []
        for i in (doc.get("items") or []):
            item_doc = frappe.get_doc("Item", i.item)
            item_group = item_doc.item_group or ""
            kitchen_group = item_doc.get("kitchen_group") or ""
            kg_name = ""
            if kitchen_group:
                kg_name = frappe.db.get_value("Kitchen Group", kitchen_group, "group_name") or ""
            items.append({
                "item": i.item, "item_name": i.item_name or i.item,
                "qty": i.qty, "rate": i.rate,
                "group": item_group, "kitchen_group": kg_name,
                "notes": i.get("notes") or ""
            })
        o["items_json"] = json.dumps(items)
    return {"orders": orders}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_processing():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Processing")
    return {"status": "processing", "name": order_name}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_ready():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))
    frappe.db.set_value("POS Order", order_name, "kitchen_status", "Ready")
    return {"status": "ready", "name": order_name}
@frappe.whitelist(methods=["POST"])
def mark_kitchen_served():
    order_name = frappe.local.form_dict.get("order_name")
    if not order_name:
        frappe.throw(_("Order name required"))

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
