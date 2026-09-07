"""Take Desk access off the floor roles on sites that already have them.

`ensure_roles()` used to set `desk_access` only on roles it created, so every
site installed before this patch kept whatever Frappe defaulted to — and Frappe
defaults a new Role to desk access on. Waiter and Kitchen Staff therefore had a
Frappe Desk login they have never needed: the web apps are the whole job, and
the roles exist for no other reason, so nobody can be depending on it. On a
shared floor phone that login is the ERPNext back office one tap away.

`create_roles` cannot do this — it is already marked done on every site that
has it, and a completed patch never runs again. Narrowing only: ensure_roles()
does not hand desk access back to a Cashier or Manager somebody took it from.
"""

import frappe

from luuvcrm.roles import ensure_roles


def execute():
    result = ensure_roles()
    if result["narrowed"]:
        frappe.logger().info(
            f"luuvcrm: removed desk access from {result['narrowed']}")
