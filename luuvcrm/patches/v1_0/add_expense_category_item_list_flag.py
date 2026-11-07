"""Separate the purchase item picker from inventory tracking.

Food ingredients and staff meals are repetitive purchases, so both use the
item list.  Only food ingredients remain stock-enabled.
"""

import frappe

from luuvcrm.api.expenses import ITEM_LIST_EXPENSE_CATEGORIES


def execute():
    if not frappe.db.exists("DocType", "Expense Category"):
        return
    if not frappe.db.has_column("Expense Category", "has_item_list"):
        return
    frappe.db.sql("UPDATE `tabExpense Category` SET has_item_list = 0")
    for category in ITEM_LIST_EXPENSE_CATEGORIES:
        if frappe.db.exists("Expense Category", category):
            frappe.db.set_value("Expense Category", category, "has_item_list", 1,
                                update_modified=False)
