"""Categorise every line of a supplier bill for mixed-receipt reporting.

Existing purchase bills pre-date the category column and are overwhelmingly
food/raw-material receipts, so they move into the default food bucket. New
lines are validated by ``create_purchase_bill`` and cannot be uncategorised.
"""

import frappe

from luuvcrm.api.expenses import FOOD_PURCHASE_CATEGORY


def execute():
    if not frappe.db.exists("DocType", "Expense Category"):
        return
    if not frappe.db.exists("Expense Category", FOOD_PURCHASE_CATEGORY):
        frappe.get_doc({
            "doctype": "Expense Category",
            "category_name": FOOD_PURCHASE_CATEGORY,
        }).insert(ignore_permissions=True)

    if frappe.db.has_column("Purchase Bill Item", "category"):
        frappe.db.sql(
            """
            UPDATE `tabPurchase Bill Item`
            SET category = %s
            WHERE IFNULL(category, '') = ''
            """,
            (FOOD_PURCHASE_CATEGORY,),
        )
