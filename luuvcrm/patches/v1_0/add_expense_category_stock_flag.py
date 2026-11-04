"""Mark only the purchase/ingredients category as stock-bearing.

Expense Category is shared by mixed supplier receipts and standalone expenses.
The flag lets a receipt contain both without turning Electricity, Gas, Rent,
and similar costs into Item/BOM/warehouse records.
"""

import frappe

from luuvcrm.api.expenses import FOOD_PURCHASE_CATEGORY


def execute():
    if not frappe.db.exists("DocType", "Expense Category"):
        return
    if not frappe.db.has_column("Expense Category", "in_stock"):
        return  # model sync normally adds it before post-model-sync patches
    frappe.db.sql("UPDATE `tabExpense Category` SET in_stock = 0")
    if frappe.db.exists("Expense Category", FOOD_PURCHASE_CATEGORY):
        frappe.db.set_value("Expense Category", FOOD_PURCHASE_CATEGORY,
                            "in_stock", 1, update_modified=False)

