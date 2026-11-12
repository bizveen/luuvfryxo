"""Fields that link customer offers and reward redemption to a POS order."""

import frappe

from luuvcrm import settings
from luuvcrm.setup_doctype import add_custom_fields


def execute():
    if settings.passive():
        return
    add_custom_fields()
    frappe.clear_cache(doctype="POS Order")
