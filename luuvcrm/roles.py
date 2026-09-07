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

    On an existing role it is corrected in one direction only. Taking desk
    access *off* Waiter or Kitchen Staff is a narrowing, and this app is the
    only reason those roles exist — a site that never uses the Desk cannot be
    depending on it, and leaving it on hands a shared floor phone the whole
    ERPNext back office. Turning it *on* is a widening and is never done here:
    a Cashier or a Manager whose desk access somebody deliberately removed keeps
    it removed, because an installer is the wrong place to hand out reach.
    Roles outside LUUVCRM_ROLES are not touched at all; they are not ours.
    """
    created, enabled, narrowed = [], [], []
    for role in LUUVCRM_ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({
                "doctype": "Role",
                "role_name": role,
                "desk_access": 1 if role in DESK_ROLES else 0,
            }).insert(ignore_permissions=True)
            created.append(role)
            continue
        if frappe.db.get_value("Role", role, "disabled"):
            frappe.db.set_value("Role", role, "disabled", 0)
            enabled.append(role)
        if role not in DESK_ROLES and frappe.db.get_value("Role", role, "desk_access"):
            frappe.db.set_value("Role", role, "desk_access", 0)
            narrowed.append(role)
    return {"created": created, "enabled": enabled, "narrowed": narrowed}


def desk_access_drift():
    """The app's roles whose desk access disagrees with DESK_ROLES.

    Reported as two lists because they are not the same problem. `too_wide` is
    a floor role that can open the Desk — what ensure_roles() fixes. `too_narrow`
    is a Cashier or Manager who cannot, which may well be deliberate; it is
    named rather than corrected, so a site that locked the Desk down on purpose
    is not quietly reopened by an install.
    """
    wide, narrow = [], []
    for role in LUUVCRM_ROLES:
        if not frappe.db.exists("Role", role):
            continue
        has = bool(frappe.db.get_value("Role", role, "desk_access"))
        if has and role not in DESK_ROLES:
            wide.append(role)
        elif not has and role in DESK_ROLES:
            narrow.append(role)
    return {"too_wide": wide, "too_narrow": narrow}


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


#: DocPerms on the doctypes this app owns (plus hospitality's three, which it
#: only reads). Declared here rather than in each `setup_*` because the whole
#: point of Phase 2c is that these become load-bearing: `frappe.get_list`
#: applies them, and until now `frappe.get_all` meant nobody noticed they were
#: wrong. And they were wrong in the direction that matters — a Restaurant
#: Manager could not read Purchase Bill, Expense, Wastage or POS Approval,
#: which are their own screens, while a Cashier could; and Restaurant Table was
#: manager-only, so the till would have lost its table map on the day the reads
#: flipped.
#:
#: Frappe DocPerms do not inherit. `MANAGER_ROLES` being a superset of
#: `POS_ROLES` governs this app's own `require_*` guards and nothing else, so
#: every role that needs a permission is named here explicitly.
_TILL = (CASHIER,)
_FLOOR = (WAITER,)
_KITCHEN = (KITCHEN_STAFF,)
_MANAGERS = (RESTAURANT_MANAGER, RESTAURANT_OWNER)

APP_DOCPERMS = {
    # Money out. A cashier enters a bill or an expense; only a manager decides
    # it — which is the approval flow, and why write is not granted at the till.
    "Purchase Bill":      {"read": _TILL + _MANAGERS, "create": _TILL + _MANAGERS,
                           "write": _MANAGERS, "delete": _MANAGERS},
    "Expense":            {"read": _TILL + _MANAGERS, "create": _TILL + _MANAGERS,
                           "write": _MANAGERS, "delete": _MANAGERS},
    "Wastage":            {"read": _TILL + _FLOOR + _MANAGERS,
                           "create": _TILL + _FLOOR + _MANAGERS,
                           "write": _MANAGERS, "delete": _MANAGERS},
    # Anyone on the floor may ask for an approval; only a manager answers it.
    "POS Approval":       {"read": _TILL + _FLOOR + _MANAGERS,
                           "create": _TILL + _FLOOR + _MANAGERS,
                           "write": _MANAGERS, "delete": _MANAGERS},
    "POS Cash Movement":  {"read": _TILL + _MANAGERS, "create": _TILL + _MANAGERS,
                           "write": _MANAGERS, "delete": _MANAGERS},
    # The staff trail and the error log are about staff, so they are manager
    # reading only. Nobody writes them through permissions — the server does,
    # with ignore_permissions, which is the point of an append-only trail.
    "LF Activity Log":    {"read": _MANAGERS},
    "LF Client Error":    {"read": _MANAGERS, "write": _MANAGERS, "delete": _MANAGERS},
    # Hospitality's, and read-only to us. The till, the floor and the kitchen
    # all need the table map; without it the POS has nothing to seat anyone at.
    "Restaurant":         {"read": _TILL + _FLOOR + _KITCHEN + _MANAGERS},
    "Restaurant Table":   {"read": _TILL + _FLOOR + _KITCHEN + _MANAGERS,
                           "write": _TILL + _MANAGERS},
    "Restaurant Menu":    {"read": _TILL + _FLOOR + _KITCHEN + _MANAGERS},
    # Reference data the screens resolve against.
    "Expense Category":   {"read": _TILL + _MANAGERS},
    "POS Item Addon":     {"read": _TILL + _FLOOR + _MANAGERS},
    "Tour Guide":         {"read": _TILL + _MANAGERS, "create": _MANAGERS,
                           "write": _MANAGERS},
    "Guide Cashback Txn": {"read": _TILL + _MANAGERS},
    "Loyalty Customer":   {"read": _MANAGERS},
}


#: Permissions to take *away*, each one because the app's own guards can be
#: walked around while it stands. Frappe exposes every doctype over its generic
#: REST API, where DocPerms are the only thing in the way — so a rule enforced
#: only in an endpoint is advisory.
#:
#: Verified, not assumed: with `Cashier: rwcd` on POS Approval a cashier could
#: load their own pending request, set `status` to "approved" and save it,
#: skipping `resolve_approval` and `block_self_approval` entirely — the same
#: hole as "a cashier could approve their own discount", through a different
#: door — and could then delete the record of having done it.
#:
#: This list is deliberately short. `ensure_app_docperms()` otherwise only ever
#: adds, because taking a permission away is how a screen goes blank in the
#: middle of service; every entry here is a case where leaving it costs more.
APP_DOCPERM_REVOKE = {
    # Raise a request, do not answer it, and do not erase it.
    "POS Approval":       {CASHIER: ("write", "delete"), WAITER: ("write", "delete")},
    "Purchase Bill":      {CASHIER: ("write", "delete")},
    "Expense":            {CASHIER: ("write", "delete")},
    # Trails. The person a record is about must not be able to edit or drop it.
    "Wastage":            {CASHIER: ("write", "delete"), WAITER: ("write", "delete")},
    "POS Cash Movement":  {CASHIER: ("write", "delete")},
    # Money. `cashback_balance` is what the restaurant owes a guide, and the
    # txn rows are its ledger; the till reads both and writes neither.
    "Tour Guide":         {CASHIER: ("write", "create", "delete")},
    "Guide Cashback Txn": {CASHIER: ("write", "create", "delete")},
}


def revoke_app_docperms():
    """Take away the permissions in APP_DOCPERM_REVOKE. Idempotent.

    The app's own writes to these doctypes go through `ignore_permissions`,
    which is what makes this safe: the till keeps working through the endpoints
    that are meant to be its only way in.
    """
    from frappe.permissions import update_permission_property

    revoked = {}
    for doctype, by_role in APP_DOCPERM_REVOKE.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        for role, perms in by_role.items():
            if not frappe.db.exists("Role", role):
                continue
            for perm in perms:
                if _role_has(doctype, role, perm):
                    update_permission_property(doctype, role, 0, perm, 0)
                    revoked.setdefault(doctype, []).append(f"{role}:{perm}")
    if revoked:
        frappe.clear_cache()
    return revoked


def _role_has(doctype, role, perm):
    rows = frappe.get_all("Custom DocPerm", filters={"parent": doctype, "role": role},
                          fields=[perm])
    if not rows:
        rows = frappe.get_all("DocPerm", filters={"parent": doctype, "role": role},
                              fields=[perm])
    return any(r.get(perm) for r in rows)


def walkable_docperms():
    """Permissions APP_DOCPERM_REVOKE says must be gone and that are still
    there. Each one is a guard that can be stepped around."""
    still = {}
    for doctype, by_role in APP_DOCPERM_REVOKE.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        for role, perms in by_role.items():
            for perm in perms:
                if frappe.db.exists("Role", role) and _role_has(doctype, role, perm):
                    still.setdefault(doctype, []).append(f"{role}:{perm}")
    return still


def ensure_app_docperms():
    """Grant the DocPerms in APP_DOCPERMS. Idempotent.

    Only ever adds. A permission somebody granted by hand for a reason this
    module does not know about is left alone, because taking one away is how a
    screen goes blank in the middle of service.
    """
    from frappe.permissions import add_permission, update_permission_property

    granted = {}
    for doctype, by_perm in APP_DOCPERMS.items():
        if not frappe.db.exists("DocType", doctype):
            continue        # created by a setup_* nobody has run on this site yet
        for perm, role_names in by_perm.items():
            for role in role_names:
                if not frappe.db.exists("Role", role):
                    continue
                add_permission(doctype, role, 0)
                update_permission_property(doctype, role, 0, perm, 1)
                granted.setdefault(doctype, set()).add(f"{role}:{perm}")
    if granted:
        frappe.clear_cache()
    return {dt: sorted(v) for dt, v in granted.items()}


def missing_docperms():
    """What APP_DOCPERMS asks for and the site does not have.

    The check to run before flipping a read from `get_all` to `get_list`: that
    flip makes the permission table load-bearing everywhere at once, and a gap
    shows up as a blank screen rather than an error.
    """
    from frappe.permissions import get_all_perms

    missing = {}
    for doctype, by_perm in APP_DOCPERMS.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        for perm, role_names in by_perm.items():
            for role in role_names:
                if not frappe.db.exists("Role", role):
                    continue
                rows = frappe.get_all("Custom DocPerm",
                                      filters={"parent": doctype, "role": role},
                                      fields=[perm]) or \
                    frappe.get_all("DocPerm", filters={"parent": doctype, "role": role},
                                   fields=[perm])
                if not any(r.get(perm) for r in rows):
                    missing.setdefault(doctype, []).append(f"{role}:{perm}")
    return missing


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

# ─── Separation of duties ────────────────────────────────────────────────────
# Not a role, but the same kind of rule: it says who may act, and it is asked
# from three places (approvals, purchases, expenses). It lived in api.py, and
# putting it here is also what lets those three stop importing each other.


def block_self_approval(requested_by):
    """Separation of duties: whoever raised a request cannot be the one who
    answers it.

    This used to apply to purchase bills only, on the reasoning that a cashier
    asking for a discount on their own sale is a smaller thing than entering
    money paid to a vendor. But resolve_approval's own gate is a POS role, not a
    manager one — so a cashier could grant their own discount, which is the one
    thing the approval flow exists to prevent. It now covers every kind.

    No role is exempt: a manager who raises a request still needs a second pair
    of eyes. Answering somebody else's request is unchanged, so a shift with two
    people on it can still work without a manager present.
    """
    if requested_by and requested_by == frappe.session.user:
        frappe.throw(_("You raised this request — someone else has to answer it"),
                     frappe.PermissionError)
