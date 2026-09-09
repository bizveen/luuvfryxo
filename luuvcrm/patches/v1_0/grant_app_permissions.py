"""Grant the DocPerms this app's own screens need.

Until now nothing read through `frappe.get_list`, so the permission table was
decoration and nobody noticed it was wrong. It was wrong in the direction that
matters: a Restaurant Manager could not read Purchase Bill, Expense, Wastage or
POS Approval — their own screens — while a Cashier could, and Restaurant Table
was manager-only, so the till would have lost its table map the moment a read
started applying permissions.

This is the prerequisite the roadmap names for Phase 2c: get the DocPerms right,
then flip the reads. Not the other way round.
"""

import frappe

from luuvcrm import roles
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    granted = roles.ensure_app_docperms()
    if granted:
        frappe.logger().info(f"luuvcrm: granted app docperms {sorted(granted)}")

    revoked = roles.revoke_app_docperms()
    if revoked:
        # Loud: this takes access away, and the reason is a hole rather than
        # tidiness. A cashier could answer their own approval by writing the row
        # directly, and then delete the record of it.
        frappe.logger().warning(f"luuvcrm: revoked walkaround docperms {revoked}")

    missing = roles.missing_docperms()
    if missing:
        # Loud: a gap here becomes a blank screen once the reads flip, not an
        # error anybody can trace back to permissions.
        frappe.logger().warning(f"luuvcrm: docperms still missing after patch: {missing}")
