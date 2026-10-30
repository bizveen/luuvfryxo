"""Install the references/counters used by BOM-backed stock movements."""

import frappe

from luuvcrm import settings
from luuvcrm.setup_doctype import add_custom_fields


def execute():
    if settings.passive():
        return
    add_custom_fields()
    frappe.clear_cache()
