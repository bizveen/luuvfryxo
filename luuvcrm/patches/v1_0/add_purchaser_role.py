"""Create the Purchaser role, and give it what /purchases needs, on a site that
already exists.

`create_roles` has already run on the live site, and a patch runs once — so a
role added to `roles.LUUVCRM_ROLES` afterwards reaches production through a new
patch and no other way. `ensure_roles()` is idempotent and only ever creates or
re-enables; `ensure_app_docperms()` only ever adds. Nobody loses anything here.

Nobody *gains* anything either until somebody is given the role in
/admin → Staff. What does change on deploy is that a cashier who is not also a
Purchaser can no longer call the purchase endpoints — which no screen of theirs
ever did, because /purchases was already manager-only.
"""

import frappe

from luuvcrm import roles, settings


def execute():
    if settings.passive():
        return  # a site that only carries the app; see settings.KEYS "passive"
    result = roles.ensure_roles()
    if result["created"]:
        frappe.logger().info(f"luuvcrm: created roles {result['created']}")
    granted = roles.ensure_app_docperms()
    if granted:
        frappe.logger().info(f"luuvcrm: granted app docperms {sorted(granted)}")
