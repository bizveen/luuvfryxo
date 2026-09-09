"""Who works here: the people, their role, their branch, and their way in.

Until this, adding a waiter meant opening the Frappe Desk, creating a User,
finding the right role in a list of ninety, and knowing that a User Permission
on Restaurant is what keeps a branch manager to one branch. That is a developer's
job description, not a restaurant owner's.

**No password is ever typed here.** Creating an account and choosing a password
for somebody else are different acts: the first belongs to the owner, the second
belongs to the person. `staff_invite_link` returns Frappe's own one-time
password link — the owner sends it, the staffer sets their own password, and it
expires on its own. Nothing on this screen accepts, stores or displays a
password.

**System Manager is refused outright.** It means "can change how the system
works" and it bypasses every guard in `roles.py`; it belongs to whoever
maintains the software, not to anyone this screen can hire (roles.py explains
the split). A screen that could grant it would make every other rule advisory.
"""

import frappe
from frappe import _
from frappe.utils import validate_email_address

from luuvcrm import activity, branch, roles

#: Roles this screen may grant. LUUVCRM_ROLES and nothing else — deliberately
#: not "every role on the site", which is how somebody ends up an Accounts
#: Manager because the list was alphabetical.
GRANTABLE = roles.LUUVCRM_ROLES


def _describe(user):
    d = frappe.db.get_value("User", user, ["name", "full_name", "enabled", "user_type"],
                            as_dict=True) or frappe._dict()
    held = set(frappe.get_roles(user))
    return {
        "user": user,
        "full_name": d.get("full_name") or user.split("@")[0],
        "enabled": int(d.get("enabled") or 0),
        "roles": [r for r in GRANTABLE if r in held],
        "is_admin": bool(held & set(roles.ADMIN_ROLES)),
        "branches": branch.assigned(user),
        "last_active": str(frappe.db.get_value("User", user, "last_active") or ""),
    }


@frappe.whitelist()
def get_staff():
    """Everyone who holds one of this app's roles, plus what they can reach.

    Reading is manager-level: a manager covering a shift needs to know who is
    on the floor. Every write below is the owner's.
    """
    roles.require_manager()
    people = []
    for row in frappe.get_all("Has Role", filters={"role": ["in", list(GRANTABLE)],
                                                   "parenttype": "User"},
                              fields=["parent"], limit_page_length=0):
        if row.parent not in ("Administrator", "Guest") and row.parent not in [p["user"] for p in people]:
            people.append(_describe(row.parent))
    people.sort(key=lambda p: (not p["enabled"], p["full_name"].lower()))
    return {
        "staff": people,
        "roles": list(GRANTABLE),
        "restaurants": branch.all_restaurants(),
        "can_edit": roles.is_owner(),
        # Named so the screen can say why a row is not editable rather than
        # showing a control that will be refused.
        "me": frappe.session.user,
    }


def _guard_write(user=None):
    roles.require_owner()
    if user and user in ("Administrator", "Guest"):
        frappe.throw(_("{0} is not staff").format(user))


def _refuse_admin_roles(role):
    if role in roles.ADMIN_ROLES:
        frappe.throw(_("System Manager is not a role this screen can grant. "
                       "It bypasses every permission in the app and belongs to "
                       "whoever maintains the software."))
    if role not in GRANTABLE:
        frappe.throw(_("{0} is not one of this app's roles").format(role))


@frappe.whitelist(methods=["POST"])
def create_staff(email=None, full_name=None, role=None, restaurant=None):
    """Add a person. No password: `staff_invite_link` is how they get in."""
    _guard_write()
    email = (email or "").strip().lower()
    full_name = (full_name or "").strip()
    if not email:
        frappe.throw(_("Enter an email address"))
    validate_email_address(email, throw=True)
    if not full_name:
        frappe.throw(_("Enter a name"))
    role = (role or "").strip()
    _refuse_admin_roles(role)
    if frappe.db.exists("User", email):
        frappe.throw(_("{0} already has an account — give them the role instead").format(email))

    parts = full_name.split(" ", 1)
    doc = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
        "send_welcome_email": 0,   # the invite link is the way in; see the module docstring
        "user_type": "System User",
        "roles": [{"role": role}],
    })
    doc.insert(ignore_permissions=True)

    if restaurant:
        branch.assign(email, restaurant)

    activity.log_activity(
        "staff.create", label="Added %s as %s" % (full_name, role),
        ref_doctype="User", ref_name=email,
        detail={"role": role, "restaurant": restaurant or None})
    return {"ok": True, "user": email, "staff": _describe(email)}


@frappe.whitelist(methods=["POST"])
def set_staff_role(user=None, role=None, on=1):
    """Give or take one of this app's roles."""
    _guard_write(user)
    if not frappe.db.exists("User", user):
        frappe.throw(_("No such user"))
    _refuse_admin_roles((role or "").strip())
    on = str(on).strip().lower() not in ("", "0", "false", "no")

    if not on and user == frappe.session.user and role == roles.RESTAURANT_OWNER:
        # The only door out of the building. Somebody has to keep the key.
        frappe.throw(_("You cannot take the Owner role off yourself"))

    doc = frappe.get_doc("User", user)
    # Saved under our own rule, not Frappe's. A Restaurant Owner deliberately has
    # no write permission on User — giving them one would let them grant
    # System Manager from the Desk, which is the single thing this module exists
    # to refuse. require_owner() above is the gate; this is how the write lands.
    doc.flags.ignore_permissions = True
    if on:
        doc.add_roles(role)
    else:
        doc.remove_roles(role)

    activity.log_activity(
        "staff.role", label="%s %s %s" % (user, "is now" if on else "is no longer", role),
        ref_doctype="User", ref_name=user, item=role,
        detail={"role": role, "granted": bool(on)})
    return {"ok": True, "staff": _describe(user)}


@frappe.whitelist(methods=["POST"])
def set_staff_branch(user=None, restaurant=None):
    """Restrict someone to one branch, or (with no restaurant) to none.

    A User Permission on Restaurant is the whole mechanism — Frappe applies it
    to every doctype with a Restaurant link, on every read. Clearing it does not
    make somebody an owner; it makes them unrestricted, which for a cashier is
    only ever a mistake, so the screen asks for a branch.
    """
    _guard_write(user)
    if not frappe.db.exists("User", user):
        frappe.throw(_("No such user"))
    if roles.is_owner(user) and restaurant:
        frappe.throw(_("An owner sees every branch — restricting one would quietly demote them"))

    if restaurant:
        branch.assign(user, restaurant)
    else:
        branch.unassign(user)

    activity.log_activity(
        "staff.branch",
        label="%s → %s" % (user, restaurant or "every branch"),
        ref_doctype="User", ref_name=user,
        detail={"restaurant": restaurant or None})
    return {"ok": True, "staff": _describe(user)}


@frappe.whitelist(methods=["POST"])
def set_staff_enabled(user=None, enabled=1):
    """Turn an account off. Never delete: every order, shift and trail row names
    its user, and deleting the User takes the name off all of it."""
    _guard_write(user)
    if not frappe.db.exists("User", user):
        frappe.throw(_("No such user"))
    on = str(enabled).strip().lower() not in ("", "0", "false", "no")
    if not on and user == frappe.session.user:
        frappe.throw(_("You cannot switch off your own account"))

    frappe.db.set_value("User", user, "enabled", 1 if on else 0)
    activity.log_activity(
        "staff.enabled", label="%s %s" % (user, "enabled" if on else "switched off"),
        ref_doctype="User", ref_name=user, detail={"enabled": int(on)})
    return {"ok": True, "staff": _describe(user)}


@frappe.whitelist(methods=["POST"])
def staff_invite_link(user=None):
    """A one-time link the staffer uses to set their own password.

    Frappe's own reset key, generated and returned rather than emailed, because
    a restaurant hires people who do not read email — the owner sends this on
    WhatsApp. It expires the way every reset link on the site does, and it is
    the only credential path this screen has.
    """
    _guard_write(user)
    if not frappe.db.exists("User", user):
        frappe.throw(_("No such user"))
    doc = frappe.get_doc("User", user)
    if not doc.enabled:
        frappe.throw(_("Switch the account back on first"))
    # `reset_password`, the public one: `_reset_password` is not on User in
    # Frappe 15.101 (the live version), and the rehearsal found it.
    link = doc.reset_password(send_email=False)

    activity.log_activity(
        "staff.invite", label="Invite link issued for %s" % user,
        ref_doctype="User", ref_name=user)
    # The link is a credential: it is returned to the caller and written to no
    # log — activity rows carry the fact, never the key.
    return {"ok": True, "user": user, "link": link}
