"""Give a live site the expense categories the profit-and-loss statement needs.

`seed_expense_categories` only fills an *empty* table, so a site that has been
taking expenses since before these existed would never get them. This tops such a
site up with the eight the statement has lines for.

Skipped when the table is empty: that is a fresh site (its seed already carries
these) or a manager who deleted every category on purpose, and neither wants them
put back. A category that already exists is left alone, disabled or not.
"""

import frappe

from luuvcrm.api.expenses import PNL_EXPENSE_CATEGORIES


def execute():
    if not frappe.db.exists("DocType", "Expense Category"):
        return
    if not frappe.db.count("Expense Category"):
        return
    for name in PNL_EXPENSE_CATEGORIES:
        if frappe.db.exists("Expense Category", name):
            continue
        frappe.get_doc({"doctype": "Expense Category", "category_name": name}).insert(
            ignore_permissions=True)
