"""Add the `restaurant` Link field to every doctype that belongs to a branch.

Additive and safe on live data. Adding the field changes nothing anyone can
see: every existing row is NULL, and while `apply_strict_user_permissions` is
off a NULL link stays visible to everyone (`db_query.py` builds
`ifnull(`tab...`.`restaurant`, '')='' or ...`).

**The field only.** This patch used to call `branch.backfill()` too, which
rewrites every historical row across seven doctypes — a data migration, not a
deploy, and the one thing docs/DEPLOY_RUNBOOK.md §0 says a deploy must never
do. A migrate that rewrites ~4,700 rows on a live site cannot be undone by
checking out the previous commit. The backfill is its own day, its own backup
and one command: `bench --site <site> execute luuvcrm.branch.backfill`.

ROADMAP Phase 2c.
"""

import frappe

from luuvcrm import branch
from luuvcrm import settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    result = branch.ensure_branch_field()
    if result["added"]:
        frappe.logger().info(f"luuvcrm: added branch field to {result['added']}")
    if result["skipped"]:
        # Not an error: these doctypes are still created at runtime by a setup_*
        # endpoint, so a site nobody has run it on has nothing to add to yet.
        frappe.logger().info(f"luuvcrm: no branch field for absent {result['skipped']}")

    left = branch.unbranched()
    if left:
        # Expected on an existing site, and deliberately left alone here.
        frappe.logger().info(
            f"luuvcrm: rows with no branch yet {left} — "
            "run luuvcrm.branch.backfill on a day of its own")
