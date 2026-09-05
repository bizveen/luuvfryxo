"""Which restaurant a row belongs to, and which one this session is looking at.

The goal (ROADMAP Phase 2c) is 2–3 restaurants, an Owner who sees all of them
and a manager who sees only their own — using Frappe's own machinery rather than
hand-written filters: a `restaurant` Link field on each doctype, a User
Permission per manager, and `frappe.get_list`, which applies both.

This module is the first half of that: the field, and the answer to "whose
branch is this?". The filtering half — flipping 51 `get_all` reads to
`get_list` — is deliberately not here, because it switches DocPerms on
everywhere at once and needs the permission table right first.

**What it replaces.** Fourteen call sites asked `frappe.get_value("Restaurant",
{}, "name")` — *the first restaurant, whichever it is*. With one restaurant that
is correct by accident. With two it silently serves one branch's menu, prices
and service charge to the other, and nothing anywhere would say so.

Lives at the top level next to `roles.py`, not inside `luuvcrm/api/`, because
the page context builders ask it too and `activity.py` cannot import the api
package. Same reason `roles.py` is where it is.
"""

import frappe

#: The Link field this module adds and reads.
FIELD = "restaurant"

#: Every doctype that belongs to a branch. `Restaurant Table` already links to
#: `Restaurant` and is hospitality's, so it is not ours to add a field to.
BRANCHED_DOCTYPES = (
    "POS Order",
    "Purchase Bill",
    "Expense",
    "Wastage",
    "POS Approval",
    "POS Cash Movement",
    "LF Activity Log",
)


# ─── Whose branch is this? ───────────────────────────────────────────────────

def current(user=None):
    """The Restaurant this session belongs to, or None.

    In order, because each step is a stronger statement than the next:

    1. **A User Permission on `Restaurant`.** This is Frappe's own answer to
       "which branch is this user's", and it is what will drive the filtering
       when the reads flip to `get_list` — so it must also drive the default,
       or a row would be written into a branch its author cannot then read.
    2. **The open shift's POS Profile**, through its company. A cashier on a
       till is unambiguously at that till's branch.
    3. **A POS Profile assigned to the user**, the same hop. Covers a manager
       who is not on a shift.
    4. **The only Restaurant, if there is exactly one.** Today's behaviour,
       preserved exactly, which is what makes this change safe to land before
       the second restaurant exists.

    There is deliberately no "first of several" step. That is the bug this
    module exists to remove: it cannot be right, and it fails silently.
    """
    user = user or frappe.session.user

    allowed = _permitted_restaurants(user)
    if len(allowed) == 1:
        return allowed[0]

    for profile in _profiles_for(user):
        restaurant = _restaurant_for_profile(profile)
        if restaurant and (not allowed or restaurant in allowed):
            return restaurant

    only = _the_only_restaurant()
    if only and (not allowed or only in allowed):
        return only
    return None


def _permitted_restaurants(user):
    """Restaurants this user holds a User Permission for. Empty means no
    restriction — which is exactly how an Owner sees every branch: give them
    the role and no User Permission, and there is no special case in the code.
    """
    if user in ("Administrator", "Guest"):
        return []
    return frappe.get_all("User Permission",
                          filters={"user": user, "allow": "Restaurant"},
                          pluck="for_value")


def _profiles_for(user):
    """POS Profiles this user could be working at — the open shift's first."""
    profiles = []
    if user not in ("Guest",):
        open_shift = frappe.db.get_value("POS Opening Entry",
                                         {"status": "Open", "user": user}, "pos_profile")
        if open_shift:
            profiles.append(open_shift)
        profiles += frappe.get_all("POS Profile User", filters={"user": user},
                                   pluck="parent")
    return [p for p in profiles if p]


def _restaurant_for_profile(profile):
    """POS Profile → company → the Restaurant on that company.

    `Restaurant.company` is hospitality's own field, so the recommended
    one-Company-per-branch model needs no schema of ours. With every branch on
    one company this hop is ambiguous and returns nothing rather than guessing —
    the caller falls through to the next step.
    """
    company = frappe.db.get_value("POS Profile", profile, "company")
    if not company:
        return None
    matches = frappe.get_all("Restaurant", filters={"company": company}, pluck="name")
    return matches[0] if len(matches) == 1 else None


def _the_only_restaurant():
    names = frappe.get_all("Restaurant", pluck="name", limit_page_length=2)
    return names[0] if len(names) == 1 else None


def all_restaurants():
    return frappe.get_all("Restaurant", pluck="name", order_by="name asc")


# ─── Stamping it on a row ────────────────────────────────────────────────────

def stamp(doc, method=None):
    """`before_insert` hook: fill in the branch if the caller did not.

    Never a dropdown the cashier has to remember — a field somebody has to set
    is a field that ends up empty, and an empty branch is a row that stays
    visible to every branch (see the `ifnull` note in ROADMAP Phase 2c).
    """
    try:
        if doc.doctype not in BRANCHED_DOCTYPES:
            return
        if not doc.meta.has_field(FIELD) or doc.get(FIELD):
            return
        doc.set(FIELD, current())
    except Exception:
        # A row that fails to get a branch is a reporting problem. A row that
        # fails to save is a cashier who cannot sell.
        frappe.log_error(frappe.get_traceback(), "branch.stamp failed")


# ─── The field ───────────────────────────────────────────────────────────────

def ensure_branch_field():
    """Add the `restaurant` Link to every branched doctype. Idempotent.

    Additive and safe on live data: every existing row is NULL, and a NULL link
    stays visible to everyone while `apply_strict_user_permissions` is off, so
    nothing disappears from anybody's screen the day this lands. `backfill()`
    is the separate, later step.
    """
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    added, skipped = [], []
    for doctype in BRANCHED_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            skipped.append(doctype)          # created by a setup_* nobody has run yet
            continue
        if frappe.db.has_column(doctype, FIELD):
            continue
        create_custom_field(doctype, {
            "fieldname": FIELD,
            "label": "Restaurant",
            "fieldtype": "Link",
            "options": "Restaurant",
            "read_only": 1,          # set by branch.stamp(), never typed
            "search_index": 1,
        })
        added.append(doctype)

    # create_custom_field adds the column itself for a Custom DocType, but not
    # for one that lives in the repo — there the Custom Field is only metadata
    # until the next schema sync. POS Order is the one that matters, and the
    # failure is silent in the worst way: the field is in the meta, so a save
    # sets it and the value goes nowhere, and any "is everything branched?"
    # check that keys off has_column reports a clean zero.
    synced = []
    for doctype in BRANCHED_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            continue
        if frappe.get_meta(doctype).get_field(FIELD) and not frappe.db.has_column(doctype, FIELD):
            frappe.db.updatedb(doctype)
            synced.append(doctype)

    if added or synced:
        frappe.clear_cache()
    return {"added": added, "synced": synced, "skipped": skipped}


def backfill(restaurant=None):
    """Give every branchless row a branch.

    Until this has run, isolation is incomplete *precisely because* those rows
    are empty: an empty link is visible to every branch. So this is what has to
    happen between adding the field and creating the second restaurant — never
    the other way round.

    Defaults to the only restaurant, and refuses to guess when there is more
    than one: at that point somebody has to say which of them the history
    belongs to.
    """
    target = restaurant or _the_only_restaurant()
    if not target:
        return {"filled": {}, "error": "no single restaurant to backfill to"}
    if not frappe.db.exists("Restaurant", target):
        frappe.throw(f"No such Restaurant: {target}")

    filled = {}
    for doctype in BRANCHED_DOCTYPES:
        if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, FIELD):
            continue
        # "is not set" builds ifnull(`field`, '') = ''. A plain
        # {FIELD: ["in", [None, ""]]} cannot work: in SQL, NULL IN (NULL) is
        # NULL, not true, so the rows that most need filling are the ones it
        # silently misses.
        names = frappe.get_all(doctype, filters={FIELD: ["is", "not set"]}, pluck="name")
        for name in names:
            frappe.db.set_value(doctype, name, FIELD, target, update_modified=False)
        if names:
            filled[doctype] = len(names)
    frappe.db.commit()
    return {"filled": filled, "restaurant": target}


def unbranched():
    """Rows still carrying no branch, per doctype. The check to run before
    relying on the isolation, and before turning strict user permissions on."""
    out = {}
    for doctype in BRANCHED_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            continue
        if not frappe.db.has_column(doctype, FIELD):
            # Not "nothing to report" — the field is missing entirely, which is
            # every row unbranched. Skipping it here is how a half-installed
            # field reads as a clean zero.
            out[doctype] = frappe.db.count(doctype)
            continue
        count = frappe.db.count(doctype, {FIELD: ["is", "not set"]})
        if count:
            out[doctype] = count
    return out
