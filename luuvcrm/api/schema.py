"""Fields and DocTypes this app adds at runtime, and the setup_* endpoints that install them.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from luuvcrm import roles


@frappe.whitelist()
def setup_pos_shift_perms():
    """Grant the Cashier role the DocPerms needed to run shifts under Frappe security.

    Kept for the sites that already call it. The grants themselves live in
    luuvcrm.roles.ensure_pos_docperms and are applied by
    luuvcrm.patches.v1_0.grant_pos_permissions, so a migrated site has them
    whether or not anyone calls this — and the two can no longer disagree.
    """
    frappe.only_for("System Manager")
    return {"role": roles.CASHIER, "granted": roles.ensure_pos_docperms()}
@frappe.whitelist()
def setup_waiter_role(assign_to=None):
    """Create the 'Waiter' role and (optionally) assign it to staff users.

    The waiter app (/waiter) is gated on this role in www/waiter.py, and POS Order
    already authorizes it (pos_order.json). But no setup code ever created or assigned
    it — only 'Cashier' is auto-created — so in practice only System Manager worked.

    Idempotent. Pass `assign_to` as a list or comma-separated string of staff emails;
    ONLY explicit emails are ever granted the role (customer accounts from /cus must
    never receive it), so bulk-assign-all is deliberately not supported.
    """
    frappe.only_for("System Manager")
    from frappe.permissions import add_permission, update_permission_property

    role = "Waiter"
    # desk_access=0: waiters use the /waiter website, not Frappe Desk. Login still works.
    if not frappe.db.exists("Role", role):
        frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0}).insert(
            ignore_permissions=True
        )

    # Make the role's POS Order perms explicit rather than relying on migrate syncing
    # pos_order.json (read/write/create — matches the doctype's Waiter perm block).
    grants = {"POS Order": ["read", "write", "create"]}
    applied = {}
    for doctype, perms in grants.items():
        add_permission(doctype, role, 0)
        for p in perms:
            update_permission_property(doctype, role, 0, p, 1)
        applied[doctype] = perms

    # Normalize assign_to to a clean list of emails.
    if isinstance(assign_to, str):
        assign_to = frappe.parse_json(assign_to) if assign_to.strip().startswith("[") \
            else [e.strip() for e in assign_to.split(",")]
    emails = [e for e in (assign_to or []) if e and str(e).strip()]

    assigned, skipped = [], []
    for email in emails:
        if not frappe.db.exists("User", email):
            skipped.append(email)
            continue
        frappe.get_doc("User", email).add_roles(role)  # idempotent
        assigned.append(email)

    frappe.clear_cache()
    return {"role": role, "granted": applied, "assigned": assigned, "skipped": skipped}
@frappe.whitelist()
def setup_pos_order_invoice_link():
    """Point POS Order.pos_invoice at the POS Invoice doctype and add the reverse
    POS Invoice -> POS Order link field. Idempotent; avoids a full bench migrate.
    """
    frappe.only_for("System Manager")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    # POS Order.pos_invoice now links to POS Invoice (runtime override of field options)
    frappe.make_property_setter({
        "doctype": "POS Order",
        "fieldname": "pos_invoice",
        "property": "options",
        "value": "POS Invoice",
        "property_type": "Text",
    })

    # Reverse link on POS Invoice so the order shows on the invoice dashboard
    created_field = False
    if not frappe.db.has_column("POS Invoice", "pos_order"):
        create_custom_field("POS Invoice", {
            "fieldname": "pos_order",
            "label": "POS Order",
            "fieldtype": "Link",
            "options": "POS Order",
            "read_only": 1,
            "insert_after": "remarks",
        })
        created_field = True

    frappe.clear_cache()
    return {"pos_order.pos_invoice_options": "POS Invoice", "pos_invoice.pos_order_field_created": created_field}
@frappe.whitelist()
def setup_takeaway_field():
    """Add a per-line `takeaway` Check to POS Order Item (for mixed dine-in + take-away
    orders, where take-away items carry no service charge). Idempotent; no full migrate."""
    frappe.only_for("System Manager")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "takeaway"):
        create_custom_field("POS Order Item", {
            "fieldname": "takeaway",
            "label": "Take Away",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "rate",
            "in_list_view": 1,
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.takeaway_field_created": created}
@frappe.whitelist()
def setup_invoice_takeaway_field():
    """Add a per-line `takeaway` Check to POS Invoice Item so the printed receipt can show
    take-away items separately (carried from the POS Order). Idempotent; no full migrate."""
    frappe.only_for("System Manager")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Invoice Item", "takeaway"):
        create_custom_field("POS Invoice Item", {
            "fieldname": "takeaway",
            "label": "Take Away",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "rate",
            "in_list_view": 1,
        })
        created = True
    frappe.clear_cache()
    return {"pos_invoice_item.takeaway_field_created": created}
@frappe.whitelist()
def setup_split_fields():
    """Add a per-line `paid_qty` Int to POS Order Item (for bill splitting — how many of the
    line's qty have already been billed via a split). Idempotent; no full migrate."""
    frappe.only_for("System Manager")
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "paid_qty"):
        create_custom_field("POS Order Item", {
            "fieldname": "paid_qty",
            "label": "Paid Qty",
            "fieldtype": "Int",
            "default": "0",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.paid_qty_field_created": created}
def _install_sent_qty_field():
    """Add a per-line `sent_qty` Int to POS Order Item — how many of the line's qty have
    already been fired to the kitchen (printed on a KOT). Lets a re-print show only the newly
    added items. Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "sent_qty"):
        create_custom_field("POS Order Item", {
            "fieldname": "sent_qty",
            "label": "Sent Qty",
            "fieldtype": "Int",
            "default": "0",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.sent_qty_field_created": created}
@frappe.whitelist()
def setup_sent_qty_field():
    """Schema setup over HTTP. Admin only; the work is in _install_sent_qty_field."""
    frappe.only_for("System Manager")
    return _install_sent_qty_field()
def _install_item_notes_field():
    """Add a per-line `notes` Small Text to POS Order Item — the waiter's special instruction
    for that specific line (e.g. "no chili", "extra spicy"). Lets the same item appear as two
    lines with different prep notes. Idempotent; no full migrate."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    created = False
    if not frappe.db.has_column("POS Order Item", "notes"):
        create_custom_field("POS Order Item", {
            "fieldname": "notes",
            "label": "Notes",
            "fieldtype": "Small Text",
            "insert_after": "takeaway",
        })
        created = True
    frappe.clear_cache()
    return {"pos_order_item.notes_field_created": created}
@frappe.whitelist()
def setup_item_notes_field():
    """Schema setup over HTTP. Admin only; the work is in _install_item_notes_field."""
    frappe.only_for("System Manager")
    return _install_item_notes_field()
def _ensure_item_notes_field():
    """Lazily create the per-line notes column the first time a write path needs it."""
    if not frappe.db.has_column("POS Order Item", "notes"):
        _install_item_notes_field()
def _clean_item_note(value):
    """Normalise a per-line note: trim and cap length; returns '' for empty/None."""
    return (str(value or "").strip())[:280]
# ─── POS Approvals (manager sign-off for bill changes: voids, price overrides) ─
# When the POS makes a change that needs a manager (void after guest check, price
# override, etc.) it raises a POS Approval; the Manager app approves/rejects and the
# action is executed server-side. The Frappe-default way: a dedicated DocType.

def _install_pos_approval():
    """Create the `POS Approval` Custom DocType (idempotent; custom:1, no migrate)."""
    if frappe.db.exists("DocType", "POS Approval"):
        return {"created": False, "exists": True}
    if not frappe.db.exists("Role", "Cashier"):
        frappe.get_doc({"doctype": "Role", "role_name": "Cashier", "desk_access": 1}).insert(
            ignore_permissions=True)
    dt = frappe.get_doc({
        "doctype": "DocType", "name": "POS Approval", "module": "Zeloura", "custom": 1,
        "naming_rule": "Random", "autoname": "hash", "track_changes": 1,
        "fields": [
            {"fieldname": "approval_type", "label": "Type", "fieldtype": "Select",
             "options": "void\nprice\nitem\nbill\ndiscount\nedit", "in_list_view": 1},
            {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link",
             "options": "POS Order", "in_list_view": 1},
            {"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
            {"fieldname": "subhead", "label": "Subhead", "fieldtype": "Small Text"},
            {"fieldname": "details_json", "label": "Details JSON", "fieldtype": "Long Text"},
            {"fieldname": "flag_text", "label": "Flag", "fieldtype": "Data"},
            {"fieldname": "action", "label": "Action", "fieldtype": "Data"},
            {"fieldname": "action_payload", "label": "Action Payload", "fieldtype": "Long Text"},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select",
             "options": "pending\napproved\nrejected\nsuperseded", "default": "pending", "in_list_view": 1},
            {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
            {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
            {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Cashier", "read": 1, "write": 1, "create": 1},
        ],
    })
    dt.flags.ignore_permissions = True
    dt.insert()
    frappe.clear_cache()
    return {"created": True}
@frappe.whitelist()
def setup_pos_approval():
    """Schema setup over HTTP. Admin only; the work is in _install_pos_approval."""
    frappe.only_for("System Manager")
    return _install_pos_approval()
