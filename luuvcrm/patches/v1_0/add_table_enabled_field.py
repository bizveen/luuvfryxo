"""Give Restaurant Table its `enabled` flag on a site that already exists.

Same rule as add_menu_available_field: the declaration lives in
setup_doctype.CUSTOM_FIELDS, which `after_install` applies, and `after_install`
runs on a new site only. A patch is how the field reaches the live restaurant.

The default is 1, so every table a restaurant already has stays in service. The
readers also treat a missing column as enabled, so the window between deploying
the code and running the patch shows every table rather than none — the same way
round as the menu flag, and for the same reason: a POS with no tables on it is a
counter that cannot take an order.
"""

import frappe

from luuvcrm import settings
from luuvcrm.setup_doctype import add_custom_fields


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    add_custom_fields()
    frappe.clear_cache(doctype="Restaurant Table")
