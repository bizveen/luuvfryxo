"""Give POS Invoice its `tour_company` and `tour_discount` fields on a site that
already exists.

The declaration is setup_doctype.CUSTOM_FIELDS, which `after_install` applies —
on a new site only. A patch is how a field reaches the live restaurant; see
sync_custom_fields for the same rule. Both fields are empty on every existing
invoice, which is what "not booked under a company" means.
"""

import frappe

from luuvcrm.setup_doctype import add_custom_fields
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    add_custom_fields()
    frappe.clear_cache(doctype="POS Invoice")
