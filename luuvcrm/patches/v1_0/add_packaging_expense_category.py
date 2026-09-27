"""Give a live site the Packaging expense category the P&L now has a line for.

Same rules as add_pnl_expense_categories: an empty table is a fresh site (its
seed already carries it) or a manager who deleted every category on purpose,
so it is left alone; an existing Packaging, disabled or not, is left alone too.
"""

import frappe

from luuvcrm.api.expenses import PACKAGING_CATEGORY


def execute():
    if not frappe.db.exists("DocType", "Expense Category"):
        return
    if not frappe.db.count("Expense Category"):
        return
    if frappe.db.exists("Expense Category", PACKAGING_CATEGORY):
        return
    frappe.get_doc({"doctype": "Expense Category",
                    "category_name": PACKAGING_CATEGORY}).insert(ignore_permissions=True)
