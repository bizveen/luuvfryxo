"""Who may do what.

One place for the role model, because it used to live in four:

    api.py:145        POS_ROLES
    api.py:5993       MENU_MANAGER_ROLES
    activity.py:434   MANAGER_ROLES        byte-identical to the above
    www/manager.py:6  MANAGER_ROLES        byte-identical again

They drifted apart the moment anyone added a role, and activity.py could not
import api.py's copy because api.py imports activity.py. This module imports
neither, so both can use it.

The model
---------
Five business roles, plus Frappe's own administrator:

    Kitchen Staff       /kitchen — advance an order's status, nothing else
    Waiter              /waiter — take and amend their own orders
    Cashier             /pos — sell, take payment, open and close the shift
    Restaurant Manager  reports, purchasing, approvals, the menu — for THEIR branch
    Restaurant Owner    everything a manager can do, across EVERY branch
    System Manager      Frappe's technical admin: doctypes, migrations, permissions

Restaurant Owner exists for the multi-branch goal (docs/ROADMAP.md Phase 2c).
It carries no extra code: a manager is pinned to one restaurant by a Frappe
User Permission, and an Owner is simply a manager with no such permission, so
Frappe shows them every branch.

System Manager is deliberately NOT a business role. It means "can change how
the system works", and it bypasses every guard below — it belongs to whoever
installs and maintains the software, not to the person who owns the restaurant.
"""

import frappe
from frappe import _

# ── role names ────────────────────────────────────────────────────────────
KITCHEN_STAFF = "Kitchen Staff"
WAITER = "Waiter"
CASHIER = "Cashier"
RESTAURANT_MANAGER = "Restaurant Manager"
RESTAURANT_OWNER = "Restaurant Owner"
SYSTEM_MANAGER = "System Manager"
ADMINISTRATOR = "Administrator"

# Inherited from ERPNext rather than defined by this app. POS User appears in
# POS Order's DocPerms so it stays; Sales User appears nowhere except the old
# POS_ROLES set. Both are kept because a live user may still hold one and being
# locked out of the till is a worse failure than an over-broad role — revisit
# once the live site's user list can be checked (ROADMAP Phase 2a).
POS_USER = "POS User"
SALES_USER = "Sales User"

#: Frappe's administrators. Members bypass every guard in this module.
ADMIN_ROLES = frozenset({SYSTEM_MANAGER, ADMINISTRATOR})

#: Sees every branch. An Owner is a manager without a User Permission.
OWNER_ROLES = ADMIN_ROLES | {RESTAURANT_OWNER}

#: Reports, purchasing, approvals, the menu.
MANAGER_ROLES = OWNER_ROLES | {RESTAURANT_MANAGER}

#: May operate a till: sell, take payment, run a shift.
POS_ROLES = MANAGER_ROLES | {CASHIER, POS_USER, SALES_USER}

#: May take and amend orders. A cashier can do everything a waiter can.
WAITER_ROLES = POS_ROLES | {WAITER}

#: May move an order through the kitchen. Waiters mark food served, so they are
#: here too.
KITCHEN_ROLES = WAITER_ROLES | {KITCHEN_STAFF}

#: Every role this app defines, in the order a person is likely to be promoted
#: through them. Used by the installer to create them.
LUUVCRM_ROLES = (KITCHEN_STAFF, WAITER, CASHIER, RESTAURANT_MANAGER, RESTAURANT_OWNER)

#: Roles that get a Frappe Desk login. Floor staff use the web apps only.
DESK_ROLES = frozenset({CASHIER, RESTAURANT_MANAGER, RESTAURANT_OWNER})


# ── asking ────────────────────────────────────────────────────────────────
def has_any(allowed, user=None):
    """True when `user` holds at least one of `allowed`."""
    return bool(set(frappe.get_roles(user or frappe.session.user)) & set(allowed))


def is_owner(user=None):
    """True for someone who should see every branch, not just their own."""
    return has_any(OWNER_ROLES, user)


def is_manager(user=None):
    return has_any(MANAGER_ROLES, user)


# ── requiring ─────────────────────────────────────────────────────────────
def _require(allowed, message, user=None):
    if not has_any(allowed, user):
        frappe.throw(message, frappe.PermissionError)


def require_admin(user=None):
    _require(ADMIN_ROLES, _("Only a System Manager can do this"), user)


def require_owner(user=None):
    _require(OWNER_ROLES, _("Only the owner can do this"), user)


def require_manager(user=None):
    _require(MANAGER_ROLES, _("Only a manager can do this"), user)


def require_pos(user=None):
    _require(POS_ROLES, _("You are not permitted to operate the POS"), user)


def require_waiter(user=None):
    _require(WAITER_ROLES, _("Only floor staff can do this"), user)


def require_kitchen(user=None):
    _require(KITCHEN_ROLES, _("Only kitchen staff can do this"), user)


# ── installing ────────────────────────────────────────────────────────────
def ensure_roles():
    """Make this app's roles exist and be usable. Idempotent.

    Called from the patch that ships them (luuvcrm/patches/v1_0/create_roles.py)
    and, once it exists, from after_install. Roles used to be created inline by
    four different setup_* endpoints, so a fresh site only had whichever roles
    somebody had happened to trigger.

    "Usable" is not the same as "present", and the difference is not academic:
    on the test site `Restaurant Manager` existed but was disabled, and
    `frappe.get_roles()` omits disabled roles — so every manager held nothing,
    the manager app returned PermissionError on every panel, and an existence
    check saw nothing wrong. These roles are not optional decoration; the app
    does not work without them, so a disabled one is re-enabled and reported.
    Removing a manager is done by taking the role off the user, not by disabling
    a role the software requires.

    Desk access is deliberate: floor and kitchen staff work entirely in the web
    apps, so a Frappe Desk login would only widen what a shared phone can reach.
    It is set only on roles this function creates — changing it on a role a site
    already has could take away access somebody is using.
    """
    created, enabled = [], []
    for role in LUUVCRM_ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({
                "doctype": "Role",
                "role_name": role,
                "desk_access": 1 if role in DESK_ROLES else 0,
            }).insert(ignore_permissions=True)
            created.append(role)
        elif frappe.db.get_value("Role", role, "disabled"):
            frappe.db.set_value("Role", role, "disabled", 0)
            enabled.append(role)
    return {"created": created, "enabled": enabled}


#: DocPerms the till needs on ERPNext's own doctypes. POS Order's perms ship
#: with the doctype; these belong to ERPNext, so they are granted rather than
#: declared.
POS_DOCPERMS = {
    "POS Opening Entry": ("read", "write", "create", "submit"),
    "POS Closing Entry": ("read", "write", "create", "submit"),
    "POS Invoice": ("read", "write", "create", "submit"),
    "Sales Invoice": ("read", "write", "create", "submit"),
    # Everything below is read-only, and every entry is here because ERPNext
    # checks it while validating a POS Invoice — with bare frappe.has_permission
    # calls that a document's ignore_permissions flag does not bypass. Without
    # them a Cashier cannot take a payment at all, however the invoice is
    # inserted, which is what a test running as a real cashier first found.
    #
    # Account is `select` rather than `read` on purpose: that is what
    # party.get_party_account settles for (see frappe.only_has_select_perm), and
    # it is the smaller grant — enough to satisfy a link, not enough to browse
    # the ledger. This whole set is deliberately narrower than ERPNext's own
    # Accounts User role, which would open the general ledger to the counter.
    "Account": ("select",),        # party.get_party_account -> account_perm_check
    "Item": ("read",),             # get_item_details -> item.check_permission()
    "Item Price": ("read",),
    "POS Profile": ("read",),
    "Customer": ("read",),
    "Price List": ("select",),
    "Mode of Payment": ("select",),
    "Warehouse": ("select",),
    "Company": ("select",),
    "UOM": ("select",),
}


def ensure_pos_docperms(role=CASHIER):
    """Grant `role` what it needs to run a shift on ERPNext's doctypes. Idempotent.

    pos_open_shift inserts a POS Opening Entry under the caller's own
    permissions, so without these a cashier cannot start a shift at all — and
    the till is unusable from the first tap of the morning.

    This used to exist only as the setup_pos_shift_perms endpoint, which
    somebody had to remember to call. It is a patch now, so a migrated site has
    it whether anyone remembered or not; the endpoint still exists and calls
    straight through here, so the two cannot drift.
    """
    from frappe.permissions import add_permission, update_permission_property

    if not frappe.db.exists("Role", role):
        frappe.get_doc({"doctype": "Role", "role_name": role,
                        "desk_access": 1 if role in DESK_ROLES else 0
                        }).insert(ignore_permissions=True)

    granted = {}
    for doctype, perms in POS_DOCPERMS.items():
        if not frappe.db.exists("DocType", doctype):
            continue  # ERPNext not installed, or an older version
        add_permission(doctype, role, 0)
        for perm in perms:
            update_permission_property(doctype, role, 0, perm, 1)
        granted[doctype] = list(perms)
    if granted:
        frappe.clear_cache()
    return granted
