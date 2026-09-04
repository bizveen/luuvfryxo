"""Give the Cashier role what it needs to run a shift.

pos_open_shift inserts a POS Opening Entry under the caller's own permissions,
so a site where nobody called setup_pos_shift_perms had a till its cashiers
could not open. Granting it by migration removes the "somebody has to remember"
step; see docs/ROADMAP.md Phase 3.
"""

import frappe

from luuvcrm.roles import ensure_pos_docperms


def execute():
    granted = ensure_pos_docperms()
    if granted:
        frappe.logger().info(f"luuvcrm: granted Cashier POS permissions {list(granted)}")
