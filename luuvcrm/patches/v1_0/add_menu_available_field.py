"""Give Restaurant Menu Item its `available` flag on a site that already exists.

The declaration lives in setup_doctype.CUSTOM_FIELDS, which `after_install`
applies — and `after_install` runs on a new site only. A patch is how a field
reaches the live restaurant; see sync_custom_fields for the same rule.

The default is 1, so every item on every existing menu stays exactly as visible
as it was. `menu_items()` also treats a missing column as "available", so the
window between deploying the code and running the patch shows the full menu
rather than an empty one.
"""

import frappe

from luuvcrm.setup_doctype import add_custom_fields


def execute():
    add_custom_fields()
    frappe.clear_cache(doctype="Restaurant Menu Item")
