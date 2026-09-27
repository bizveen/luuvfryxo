"""Give an already-installed `Purchase Bill` its `purchase_date` and `discount` fields.

`_install_purchase_bill` only creates the DocType once — a site that had it
installed before these two fields were added to that function never gets them,
and every report that reads `purchase_date` (the P&L's food cost above all)
throws `Unknown column`. Skipped when the DocType does not exist yet: a fresh
site creates it with both fields already, via `_install_purchase_bill` itself.

An even older install left `custom` unset, which blocks editing the DocType
outside developer mode — corrected here too, since the code has always meant
this one to be a Custom DocType (see `_install_purchase_bill`).
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Purchase Bill"):
        return
    dt = frappe.get_doc("DocType", "Purchase Bill")
    existing = {f.fieldname for f in dt.fields}
    changed = not dt.custom
    dt.custom = 1
    if "purchase_date" not in existing:
        dt.append("fields", {
            "fieldname": "purchase_date", "label": "Purchase Date", "fieldtype": "Date",
            "in_list_view": 1, "search_index": 1,
        })
        changed = True
    if "discount" not in existing:
        dt.append("fields", {
            "fieldname": "discount", "label": "Discount", "fieldtype": "Currency", "default": "0",
        })
        changed = True
    if not changed:
        return
    dt.flags.ignore_permissions = True
    dt.save()
    frappe.clear_cache()
