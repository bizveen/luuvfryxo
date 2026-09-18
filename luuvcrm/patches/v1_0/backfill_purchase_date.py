"""Date every Purchase Bill that existed before `purchase_date` did.

The purchase reports now window on the day the goods were bought, not the day
the bill was typed in. A bill with no purchase_date would fall out of every one
of them, so each old bill takes the only date it has: the day it was entered,
which is what the reports used to read. Nothing already counted moves.

Runs on passive sites too — it only fills a column the model sync just made,
and a bill with a date is never worse than one without.
"""

import frappe


def execute():
    if not frappe.db.has_column("Purchase Bill", "purchase_date"):
        return
    frappe.db.sql("""
        UPDATE `tabPurchase Bill`
        SET purchase_date = DATE(COALESCE(requested_at, creation))
        WHERE purchase_date IS NULL
    """)
