"""Add the `restaurant` Link field to every doctype that belongs to a branch,
then give the existing rows a branch.

Additive and safe on live data. Adding the field changes nothing anyone can
see: every existing row is NULL, and while `apply_strict_user_permissions` is
off a NULL link stays visible to everyone (`db_query.py` builds
`ifnull(`tab...`.`restaurant`, '')='' or ...`). The backfill is what actually
completes the isolation, because it is the empty rows that leak across
branches.

The backfill deliberately refuses to guess once there is more than one
Restaurant — at that point somebody has to say which of them the history
belongs to. On a site with one, which is every site today, it just works.

ROADMAP Phase 2c.
"""

import frappe

from luuvcrm import branch


def execute():
    result = branch.ensure_branch_field()
    if result["added"]:
        frappe.logger().info(f"luuvcrm: added branch field to {result['added']}")
    if result["skipped"]:
        # Not an error: these doctypes are still created at runtime by a setup_*
        # endpoint, so a site nobody has run it on has nothing to add to yet.
        frappe.logger().info(f"luuvcrm: no branch field for absent {result['skipped']}")

    filled = branch.backfill()
    if filled.get("filled"):
        frappe.logger().info(f"luuvcrm: backfilled branch on {filled['filled']}")
    elif filled.get("error"):
        frappe.logger().warning(
            "luuvcrm: branch backfill skipped — " + filled["error"]
            + ". Run luuvcrm.branch.backfill(restaurant=...) once it is decided.")
