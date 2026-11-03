"""Allow decimal quantities for the purchase catalogue's default ``Nos`` UOM.

LuuvCRM purchase quantities are floats and many legacy catalogue items still
use ``Nos`` as their stock UOM.  Once purchase approvals began creating native
Material Receipts, ERPNext's default whole-number restriction on ``Nos`` made
those otherwise valid bills fail at approval time.

This is an intentional site-wide UOM rule: the same quantity must mean the same
thing in the custom purchase bill and in ERPNext's stock ledger.
"""

import frappe

from luuvcrm import settings


def execute():
    if settings.passive() or not frappe.db.exists("UOM", "Nos"):
        return
    if not frappe.db.get_value("UOM", "Nos", "must_be_whole_number"):
        return

    frappe.db.set_value(
        "UOM", "Nos", "must_be_whole_number", 0, update_modified=False
    )
    # TransactionBase reads the UOM through get_cached_value().  Clear this
    # document immediately so an approval in the same worker sees the new rule.
    frappe.clear_document_cache("UOM", "Nos")
