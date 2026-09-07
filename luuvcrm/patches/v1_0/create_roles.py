"""Create the LuuvCRM roles, Restaurant Owner included.

Roles were previously created inline by whichever setup_* endpoint happened to
need one, so which roles a site had depended on what somebody had clicked. This
patch is the first step of moving that whole class of work out of HTTP handlers
and into migrations (docs/ROADMAP.md Phase 3).
"""

import frappe

from luuvcrm.roles import ensure_roles


def execute():
    result = ensure_roles()
    if result["created"]:
        frappe.logger().info(f"luuvcrm: created roles {result['created']}")
    if result["enabled"]:
        # Loud, not silent: a disabled role the app requires means every holder
        # of it silently had no permissions at all.
        frappe.logger().warning(
            f"luuvcrm: re-enabled roles the app requires {result['enabled']}")
