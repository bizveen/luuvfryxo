"""The app's schema, declared once, in the repo.

Everything LuuvCRM adds on top of ERPNext + Hospitality lives here: the custom
DocTypes, the custom fields, the property setters, the roles and their DocPerms,
and the handful of reference rows the UI cannot work without.

Until now those were created by calling ~27 `setup_*` HTTP endpoints in an
undocumented order (see `docs/PRODUCTION_READINESS.md` §B1), which meant a fresh
site could not be provisioned without a checklist and two sites drifted apart
silently. Those endpoints are still there and still work — they are simply no
longer the source of truth. This module is.

Run it:

    bench --site <site> execute luuvcrm.setup_doctype.execute

It also runs automatically from `after_install` (hooks.py), so a fresh site is
provisioned by `bench install-app luuvcrm` alone.

**It has to do everything the patches do.** `frappe.installer` calls
`set_all_patches_as_completed()` on a fresh install, which marks every patch in
`patches.txt` as done *without running it* — so none of `create_roles`,
`grant_pos_permissions`, `grant_app_permissions` or `add_branch_field` fires on
a new site. `after_install` is the only thing that does. That is why `execute()`
calls into `roles` and `branch` instead of declaring permissions of its own:
whatever a migrated site gets from a patch, a fresh site has to get from here,
or the two are not the same site.

It is deliberately NOT wired to `after_migrate`. Every helper below is
create-only, so running it on a provisioned site is a no-op — but "no-op" is a
property of this code, not a guarantee the framework gives us, and a live deploy
is the wrong place to find out we were wrong. On an existing site, run
`report_drift()` first and then `execute()` by hand.

Three rules every helper here sticks to:

  * create-only  — nothing here deletes or rewrites data that already exists
  * guarded      — a field/DocType already present (standard *or* custom) is skipped
  * non-fatal    — a failure is logged and skipped, never allowed to abort the run

The DocTypes are created with `custom: 1`, matching what is on the live site
today. That keeps them out of `bench migrate`'s path: this module can be deployed
to production without changing a single row. Converting them to app-owned DocType
JSON under `luuvcrm/zeloura/doctype/` is the real fix (`PRODUCTION_READINESS.md`
§B1) and is a separate job, because it needs the live schema verified field by
field first.

NOTE: `api.setup_customer_accounts()` is deliberately not called from here — it
deletes the Customer Account and Customer Token DocTypes before recreating them,
which on a live site takes every account and every signed-in session with it.
The safe equivalent is below.
"""

import frappe

from luuvcrm import branch, roles
from luuvcrm.api import EXPENSE_CATEGORIES, GUIDE_DEFAULT_PERCENT
from luuvcrm.api.errors import _install_client_errors

# ─── Shared permission blocks ────────────────────────────────────────────────

# ─── Roles ───────────────────────────────────────────────────────────────────
# Declared in `luuvcrm/roles.py`, not here. That module owns the role model —
# five business roles including Restaurant Owner, which this file's earlier
# four-role list predates — and the DocPerms that go with them.
#
# Why execute() calls into it at all: `set_all_patches_as_completed()` marks
# every patch as done on a fresh install *without running it*, so none of
# create_roles, grant_pos_permissions, grant_app_permissions or add_branch_field
# fires on a new site. after_install is the only thing that does. Anything a
# patch provides, this module has to provide too, or `bench install-app` yields
# a site that a migrated site would never look like.


# ─── Custom DocTypes ─────────────────────────────────────────────────────────
# Order matters: a Link field's target and a child table's row type must already
# exist when the parent is created.


#: The DocTypes this app owns. Their *shape* — fields, permissions, naming —
#: lives in `luuvcrm/zeloura/doctype/<name>/<name>.json`, which Frappe syncs
#: on install and on every migrate. This list is only the roll-call, so
#: `report_drift()` can say when one of them failed to arrive.
#:
#: They used to be declared here in full and created with `custom: 1`, which
#: kept them out of migrate's path and made this file a second definition
#: that could drift from the JSON. It is not one any more.
EXPECTED_DOCTYPES = (
    "Item Review",
    "POS Item Addon",
    "Kitchen Group",
    # Not "WhatsApp Account": that DocType belongs to the frappe_whatsapp app
    # (13 fields, module "Frappe Whatsapp"). This app shipped a 5-field one of
    # the same name for a while, which would have overwritten it on the first
    # migrate. api/wa.py reads it when present and creates nothing.
    "Customer Account",
    "Customer Token",
    "Loyalty Customer",
    "Cashback Transaction",
    "Loyalty Settings",
    "POS Cash Movement",
    "POS Approval",
    "Purchase Bill Item",
    "Purchase Bill",
    "Expense Category",
    "Expense",
    "Wastage",
    "QR Promo Campaign",
    "QR Promo Code",
    "Tour Guide",
    "Tour Company",
    "Guide Token",
    "Guide Cashback Txn",
    "Guide Invite Code",
    "Guide Settings",
    "LF Activity Log",
)


# ─── Custom fields ───────────────────────────────────────────────────────────
# Fields the Python/JS code reads or writes but that no DocType JSON defines.
# Applied after the DocTypes exist, so the fields hanging off Purchase Bill and the
# Kitchen Group link have something to attach to.
# Order matters only where `insert_after` points at another row in this list.
CUSTOM_FIELDS = [
    # Kiosk / menu presentation
    {"dt": "Item", "fieldname": "speciality_tags", "label": "Speciality Tags", "fieldtype": "Small Text",
     "description": "Comma-separated: Chef's Special, Most Popular, Spicy, Vegan, Gluten-Free"},
    {"dt": "Item", "fieldname": "youtube_url", "label": "YouTube Video URL", "fieldtype": "Data"},
    {"dt": "Item", "fieldname": "prep_time", "label": "Preparation Time (mins)", "fieldtype": "Int"},
    # Link, not Data: api.get_menu() resolves it with
    # frappe.db.get_value("Kitchen Group", <this>, "group_name").
    {"dt": "Item", "fieldname": "kitchen_group", "label": "Kitchen Group", "fieldtype": "Link",
     "options": "Kitchen Group", "insert_after": "item_group"},
    # Item-wise commission override: beats the guide's own percent for that line
    # (most-specific wins).
    {"dt": "Item", "fieldname": "guide_commission_percent", "label": "Guide Commission %", "fieldtype": "Percent",
     "insert_after": "standard_rate"},

    # Restaurant settings
    {"dt": "Restaurant", "fieldname": "service_charge", "label": "Service Charge (%)", "fieldtype": "Percent",
     "description": "Service charge percentage added to all orders", "insert_after": "phone"},
    {"dt": "Restaurant", "fieldname": "guide_commission_percent", "label": "Guide Commission (%)",
     "fieldtype": "Percent", "description": "Guide commission percentage"},

    # POS Order header
    # Select, not Data: _is_takeaway() matches on "Take Away", and the option list
    # is what stops a typo from silently costing the service charge.
    {"dt": "POS Order", "fieldname": "order_type", "label": "Order Type", "fieldtype": "Select",
     "options": "Dine In\nTake Away\nDelivery", "default": "Dine In", "insert_after": "order_source"},
    {"dt": "POS Order", "fieldname": "discount_amount", "label": "Discount Amount", "fieldtype": "Currency",
     "insert_after": "service_charge_amount", "read_only": 1,
     "description": "Set by apply_discount(); not entered by hand"},
    {"dt": "POS Order", "fieldname": "discount_note", "label": "Discount Note", "fieldtype": "Data",
     "insert_after": "discount_amount", "read_only": 1},
    {"dt": "POS Order", "fieldname": "pos_opening_entry", "label": "POS Opening Entry", "fieldtype": "Link",
     "options": "POS Opening Entry", "insert_after": "pos_invoice", "read_only": 1,
     "description": "Shift this order was rung up in"},
    # Set when a guest check is printed; after that, deleting a line needs a
    # manager approval.
    {"dt": "POS Order", "fieldname": "check_printed", "label": "Guest Check Printed", "fieldtype": "Check",
     "default": "0", "insert_after": "kitchen_status"},
    # The customer's preferred time. Free-item promo orders must fall within
    # PROMO_WINDOW_HOURS of now.
    {"dt": "POS Order", "fieldname": "scheduled_time", "label": "Scheduled Time", "fieldtype": "Datetime",
     "insert_after": "check_printed"},

    # POS Order — WhatsApp notification state
    {"dt": "POS Order", "fieldname": "whatsapp_status", "label": "WhatsApp Status", "fieldtype": "Data",
     "read_only": 1},
    {"dt": "POS Order", "fieldname": "wa_message_id", "label": "WhatsApp Message ID", "fieldtype": "Data",
     "read_only": 1, "no_copy": 1},

    # POS Order lines
    {"dt": "POS Order Item", "fieldname": "takeaway", "label": "Takeaway", "fieldtype": "Check",
     "default": "0", "insert_after": "rate", "in_list_view": 1},
    {"dt": "POS Order Item", "fieldname": "sent_qty", "label": "Sent Qty", "fieldtype": "Int",
     "default": "0", "insert_after": "takeaway",
     "description": "How many of this line have already been fired to the kitchen on a KOT"},
    {"dt": "POS Order Item", "fieldname": "paid_qty", "label": "Paid Qty", "fieldtype": "Int",
     "default": "0", "insert_after": "sent_qty",
     "description": "How many of this line have already been settled (bill splitting)"},
    {"dt": "POS Order Item", "fieldname": "notes", "label": "Notes", "fieldtype": "Small Text",
     "insert_after": "paid_qty",
     "description": "Per-line prep instruction — lets the same item appear twice with different notes"},

    # Cash tendered / change returned — display-only, printed on the receipt.
    # Written by api._set_cash_tender() before the invoice is inserted; without
    # these fields Frappe silently drops the attributes and the receipt shows no
    # balance line.
    {"dt": "POS Invoice", "fieldname": "custom_tendered_amount", "label": "Tendered Amount",
     "fieldtype": "Currency", "read_only": 1, "no_copy": 1,
     "description": "Cash handed over by the customer (display only)"},
    {"dt": "POS Invoice", "fieldname": "custom_change_returned", "label": "Change Returned",
     "fieldtype": "Currency", "read_only": 1, "no_copy": 1,
     "description": "Balance given back to the customer (display only)"},
    {"dt": "Sales Invoice", "fieldname": "custom_tendered_amount", "label": "Tendered Amount",
     "fieldtype": "Currency", "read_only": 1, "no_copy": 1,
     "description": "Cash handed over by the customer (display only)"},
    {"dt": "Sales Invoice", "fieldname": "custom_change_returned", "label": "Change Returned",
     "fieldtype": "Currency", "read_only": 1, "no_copy": 1,
     "description": "Balance given back to the customer (display only)"},

    # Reverse link so the order shows on the invoice's dashboard.
    # Both invoice types, because _link_invoice_to_order() writes whichever one
    # the sale became — and because pos_order.json declares a Document Link to
    # Sales Invoice through this fieldname, so leaving it off POS Invoice's
    # sibling makes Frappe reject the field with "Unknown column
    # 'tabSales Invoice.pos_order'" and take the other 30 fields down with it.
    {"dt": "POS Invoice", "fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link",
     "options": "POS Order", "read_only": 1, "insert_after": "remarks"},
    {"dt": "Sales Invoice", "fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link",
     "options": "POS Order", "read_only": 1, "insert_after": "remarks"},

    # Invoice lines carry the takeaway flag through to the printed bill
    {"dt": "POS Invoice Item", "fieldname": "takeaway", "label": "Takeaway", "fieldtype": "Check",
     "default": "0", "insert_after": "rate", "in_list_view": 1},

    # Purchase Bill payment state (the doctype itself is created above)
    {"dt": "Purchase Bill", "fieldname": "payment_status", "label": "Payment Status", "fieldtype": "Select",
     "options": "unpaid\npaid", "default": "unpaid", "insert_after": "status"},
    {"dt": "Purchase Bill", "fieldname": "paid_at", "label": "Paid At", "fieldtype": "Datetime",
     "insert_after": "payment_status"},
    {"dt": "Purchase Bill", "fieldname": "paid_by", "label": "Paid By", "fieldtype": "Link", "options": "User",
     "insert_after": "paid_at"},

    # "Sold out today" — a menu-row flag rather than Item.disabled, because it is
    # reversible with one tap and must not stop an open bill that already has the
    # item from being priced or paid. Every screen reads it through
    # luuvcrm.api.menu.menu_items(), so it means the same thing on all of them.
    {"dt": "Restaurant Menu Item", "fieldname": "available", "label": "Available",
     "fieldtype": "Check", "default": "1", "insert_after": "rate", "in_list_view": 1,
     "description": "Off = hidden from every ordering screen. The item, its price "
                    "and its history are untouched."},

    # A table can be taken out of service without being deleted. Deleting one is
    # not an option — every past bill links to it by name — and hospitality's
    # doctype has no flag of its own, so this is ours. Default 1, so every table
    # on every site stays exactly as it is; and like `available` above, the flag
    # is read by the pickers only. A bill already open on a table that has just
    # been turned off still prices, prints and pays.
    {"dt": "Restaurant Table", "fieldname": "enabled", "label": "Enabled",
     "fieldtype": "Check", "default": "1", "insert_after": "restaurant", "in_list_view": 1,
     "description": "Off = hidden from every ordering screen and from the QR sheet. "
                    "Orders already on this table are untouched."},

    # (The three z_* storefront attributes went with zeloura_api.py. Nothing
    # declares them now; a site that already has the columns keeps them, empty
    # and unread, until somebody drops them by hand.)
]

_CF_KEYS = ("options", "description", "default", "insert_after", "read_only", "no_copy",
            "in_list_view", "in_standard_filter", "reqd", "unique", "hidden", "precision")


# ─── Property setters ────────────────────────────────────────────────────────
# Overrides on fields pos_order.json declares, kept out of the JSON so the JSON
# stays the shape the live site already has.
PROPERTY_SETTERS = [
    # POS Order.pos_invoice points at POS Invoice, not the JSON's Sales Invoice.
    {"doctype": "POS Order", "fieldname": "pos_invoice", "property": "options",
     "value": "POS Invoice", "property_type": "Text"},
    # `Draft` (auto-created, not yet fired) and `Cancelled` are real states the
    # code sets; without them insert()/save() rejects the value.
    {"doctype": "POS Order", "fieldname": "kitchen_status", "property": "options",
     "value": "Pending\nProcessing\nReady\nServed\nDraft\nCancelled", "property_type": "Text"},
]


# ─── Reference rows ──────────────────────────────────────────────────────────
# Not demo data — the screens are unusable without them. Each is seeded only when
# its table is completely empty, so a manager who deletes one does not get it back.

KITCHEN_GROUPS = [
    ("Kottu", 1, "#F59E0B"), ("Rice & Noodles", 2, "#3B82F6"), ("Curries & Gravies", 3, "#10B981"),
    ("Biryani", 4, "#8B5CF6"), ("Pizza", 5, "#EC4899"), ("Beverages", 6, "#06B6D4"),
    ("Coffee & Tea", 7, "#F97316"), ("Desserts", 8, "#14B8A6"), ("Sides & Starters", 9, "#6366F1"),
    ("Soups & Salads", 10, "#84CC16"), ("Grills & Sizzlers", 11, "#EF4444"), ("Breads", 12, "#D946EF"),
    ("Other", 99, "#6B7280"),
]

# The anonymous POS customer. Must match the literal used in api.py
# (place_order / process_payment / pay_split / kiosk_place_order).
WALK_IN = "Walk In"

# Select options appended after their DocType first shipped. Sites created by the
# old setup_* path already have them; this is only for the ones that don't.
LATE_SELECT_OPTIONS = [
    ("POS Approval", "approval_type", "expense"),
    ("Guide Cashback Txn", "type", "Redeem"),
]


def execute():
    # Roles first: the DocTypes below name them in their permission rows, and a
    # DocPerm against a role that does not exist is silently dropped.
    _step("roles", roles.ensure_roles)

    _step("DocTypes", ensure_doctypes)
    _step("custom fields", add_custom_fields)
    _step("property setters", ensure_property_setters)
    _step("select options", ensure_select_options)

    # The branch column has to exist before anything is stamped into it, and
    # LF Client Error before the first front-end error tries to report itself.
    _step("branch field", branch.ensure_branch_field)
    _step("client error log", _install_client_errors)

    # Permissions last: they name the DocTypes above, and the revoke pass has to
    # run after every grant, or it takes away what a later step puts back.
    _step("till permissions", roles.ensure_pos_docperms)
    _step("app permissions", roles.ensure_app_docperms)
    _step("walkaround permissions", roles.revoke_app_docperms)

    _step("settings single", ensure_settings_single)
    _step("settings singles", ensure_singles)
    _step("home page", ensure_home_page)
    _step("kitchen groups", seed_kitchen_groups)
    _step("expense categories", seed_expense_categories)
    _step("Walk In customer", ensure_walk_in_customer)
    frappe.clear_cache()
    frappe.db.commit()
    print("luuvcrm setup complete")


def after_install():
    """`after_install` hook (hooks.py). Fresh sites only — never runs on live."""
    execute()


# ─── Safety helpers ──────────────────────────────────────────────────────────

def _step(label, fn):
    """Run one provisioning step; log and continue if it blows up.

    No savepoint, deliberately. Almost every step here issues DDL — creating a
    DocType, adding a column — and DDL implicitly commits in MariaDB, which
    discards any open savepoint. Rolling back to one afterwards raises
    "SAVEPOINT luuvcrm_setup does not exist" and turns a step that should have
    been logged and skipped into a crashed run that takes the rest of the
    provisioning with it. A plain rollback of whatever is still uncommitted is
    both what is available and what is wanted: each step commits on success, so
    there is nothing of an earlier step left to lose.
    """
    try:
        fn()
        frappe.db.commit()
    except Exception:
        try:
            frappe.db.rollback()
        except Exception:
            pass
        frappe.log_error(frappe.get_traceback(), f"luuvcrm setup: {label}")
        print(f"  ! skipped {label} (see Error Log)")


def _has_field(dt, fieldname):
    """True if `dt` already exposes `fieldname`, whether standard or custom.

    Checking the meta as well as Custom Field matters: sites provisioned earlier
    by hand (or by a DocType JSON that gained the field later) already have some
    of these as standard fields, and inserting a Custom Field of the same name
    would fail on a duplicate fieldname."""
    if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
        return True
    try:
        return bool(frappe.get_meta(dt).get_field(fieldname))
    except Exception:
        return False


# ─── DocTypes ────────────────────────────────────────────────────────────────

def ensure_doctypes():
    """Check the DocTypes arrived. Creating them is Frappe's job now.

    `install_app` runs `sync_for()` — which imports every DocType JSON in the
    app — before it calls `after_install`, and `bench migrate` does the same on
    an existing site. So by the time this runs they should all be here, and one
    that is not means the JSON is missing or the sync failed, which is worth
    saying out loud rather than papering over by creating a second version of it
    from a Python dict.
    """
    missing = [n for n in EXPECTED_DOCTYPES if not frappe.db.exists("DocType", n)]
    if missing:
        print(f"  ! {len(missing)} DocType(s) missing after sync: {', '.join(missing)}")
        print("    run `bench --site <site> migrate` — they come from "
              "luuvcrm/zeloura/doctype/*/")
    else:
        print(f"DocTypes: all {len(EXPECTED_DOCTYPES)} present")


def ensure_select_options():
    """Append an option to a Select on an existing custom DocType.

    Frappe rejects a value that is not in `options`, so an option added after the
    DocType shipped has to be registered before the code can write it. New sites
    get these from the definitions above; this is the catch-up path."""
    for doctype, fieldname, option in LATE_SELECT_OPTIONS:
        if not frappe.db.exists("DocType", doctype):
            continue
        dt = frappe.get_doc("DocType", doctype)
        for f in dt.fields:
            if f.fieldname != fieldname:
                continue
            opts = [o for o in (f.options or "").split("\n") if o]
            if option in opts:
                break
            f.options = "\n".join(opts + [option])
            dt.flags.ignore_permissions = True
            dt.save()
            print(f"  + {doctype}.{fieldname} option '{option}'")
            break


# ─── Custom fields ───────────────────────────────────────────────────────────

def add_custom_fields():
    # ensure_doctypes() ran just before this and created DocTypes, so the meta
    # cache is stale — and _has_field() reads it. Stale in the "already there"
    # direction means a field is silently skipped and never added, which is how
    # POS Order.scheduled_time went missing while the run reported 0 failures.
    frappe.clear_cache()

    added = 0
    failed = []
    for f in CUSTOM_FIELDS:
        dt, fieldname = f["dt"], f["fieldname"]
        # The parent DocType ships with another app (hospitality, erpnext); if that
        # app is not installed on this site there is nothing to attach to.
        if not frappe.db.exists("DocType", dt):
            continue
        if _has_field(dt, fieldname):
            continue
        cf = {"doctype": "Custom Field", "dt": dt, "fieldname": fieldname,
              "label": f["label"], "fieldtype": f["fieldtype"]}
        cf.update({k: f[k] for k in _CF_KEYS if k in f})
        # Per field, not per step. `_step` wraps the whole call in one savepoint,
        # so without this a single bad declaration rolls back every field that
        # already succeeded and prints one "skipped" line for all of it — which
        # is exactly what a missing Sales Invoice.pos_order did the first time
        # this module was ever run.
        try:
            frappe.get_doc(cf).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            failed.append(f"{dt}.{fieldname}")
            frappe.log_error(frappe.get_traceback(), f"luuvcrm setup: field {dt}.{fieldname}")
            print(f"  ! {dt}.{fieldname} (see Error Log)")
            continue
        print(f"  + {dt}.{fieldname}")
        added += 1
    print(f"custom fields: {added} added, {len(CUSTOM_FIELDS) - added - len(failed)} "
          f"already present or N/A, {len(failed)} failed")
    if failed:
        print(f"  failed: {', '.join(failed)}")


def ensure_property_setters():
    """make_property_setter is itself idempotent — it updates the existing setter
    rather than inserting a duplicate."""
    for ps in PROPERTY_SETTERS:
        if not frappe.db.exists("DocType", ps["doctype"]):
            continue
        # validate_fields_for_doctype=False: kitchen_status' options are being
        # widened, and the validator would check the new list against live rows.
        frappe.make_property_setter(dict(ps), validate_fields_for_doctype=False)


# ─── Reference rows ──────────────────────────────────────────────────────────

def ensure_settings_single():
    """The `LuuvCRM Settings` single — everything the restaurant owns.

    A Single DocType so the owner changes their own brand name and WhatsApp
    number in the Desk, without a deploy and without a developer. The fields are
    generated from `settings.KEYS`, so adding a setting means adding one row there
    and nothing else. See luuvcrm/settings.py for why some keys live in
    site_config.json instead.
    """
    from luuvcrm import settings

    owner_keys = [k for k in settings.KEYS if k.home == settings.OWNER]

    def _field(k):
        return {"fieldname": k.name, "label": k.name.replace("_", " ").title(),
                "fieldtype": "Data", "description": k.about}

    if not frappe.db.exists("DocType", settings.SETTINGS_DOCTYPE):
        frappe.get_doc({
            "doctype": "DocType", "name": settings.SETTINGS_DOCTYPE,
            "module": "Zeloura", "custom": 1, "issingle": 1,
            "fields": [_field(k) for k in owner_keys],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1,
                             "create": 1, "delete": 1},
                            {"role": "Restaurant Owner", "read": 1, "write": 1}],
        }).insert(ignore_permissions=True)
        print(f"  + DocType {settings.SETTINGS_DOCTYPE}")

    else:
        # A key added to settings.KEYS after the DocType was created has no
        # field yet, and settings.get() would quietly keep returning the
        # default with no way to change it. The promise in settings.py is that
        # adding a setting is one row there and nothing else; this is what
        # keeps it.
        doc = frappe.get_doc("DocType", settings.SETTINGS_DOCTYPE)
        have = {f.fieldname for f in doc.fields}
        added = [k for k in owner_keys if k.name not in have]
        if added:
            for k in added:
                doc.append("fields", _field(k))
            doc.flags.ignore_permissions = True
            doc.save(ignore_permissions=True)
            print(f"  + {settings.SETTINGS_DOCTYPE}: "
                  + ", ".join(k.name for k in added))

    # Seed each field with the declared default, once. Only when empty: a blank
    # here means "not set" and falls back to the same default anyway, so this is
    # for the owner's benefit — they see what the value is instead of a blank
    # box they have to guess at.
    doc = frappe.get_single(settings.SETTINGS_DOCTYPE)
    changed = False
    for k in owner_keys:
        if not doc.get(k.name):
            doc.set(k.name, k.default)
            changed = True
    if changed:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        settings.forget()
        print(f"  = {settings.SETTINGS_DOCTYPE} seeded with defaults")


def ensure_home_page():
    """Point `/` at the restaurant's landing page — on a site that has not
    chosen one, and on one still pointing at `app2`, a page deleted in the
    September cleanup.

    Website Settings, not a `home_page` hook: hooks are bench-wide and this app
    is installed on a site that is not a restaurant, whose front door is its
    own. A site that has set its home page to anything else is left alone.
    """
    current = frappe.db.get_single_value("Website Settings", "home_page")
    if current and current != "app2":
        return
    frappe.db.set_single_value("Website Settings", "home_page", "home")
    frappe.clear_cache()
    print("  = Website Settings.home_page = home")


def ensure_singles():
    """Materialise the settings singles so `frappe.get_doc(...)` has a row to read."""
    for single in ("Loyalty Settings", "Guide Settings"):
        if not frappe.db.exists("DocType", single):
            continue
        if frappe.db.exists(single, single):
            continue
        doc = frappe.get_doc({"doctype": single})
        doc.flags.ignore_permissions = True
        doc.insert()
        print(f"  + {single}")


def seed_kitchen_groups():
    """Sri Lankan restaurant preset. Only when the table is completely empty —
    a site that has built its own groups is left alone."""
    if not frappe.db.exists("DocType", "Kitchen Group") or frappe.db.count("Kitchen Group"):
        return
    for name, order, color in KITCHEN_GROUPS:
        doc = frappe.get_doc({"doctype": "Kitchen Group", "group_name": name,
                              "display_order": order, "color": color})
        doc.flags.ignore_permissions = True
        doc.insert()
    print(f"  + {len(KITCHEN_GROUPS)} kitchen groups")


def seed_expense_categories():
    """Same empty-table guard: a manager who deletes a category does not get it back."""
    if not frappe.db.exists("DocType", "Expense Category") or frappe.db.count("Expense Category"):
        return
    for c in EXPENSE_CATEGORIES:
        frappe.get_doc({"doctype": "Expense Category", "category_name": c}).insert(ignore_permissions=True)
    print(f"  + {len(EXPENSE_CATEGORIES)} expense categories")


def ensure_walk_in_customer():
    """The anonymous customer every POS sale is booked against.

    api.py hardcodes "Walk In" in four places (place_order, process_payment,
    pay_split and the kiosk path); nothing created it, so a site that never had
    it added by hand failed at invoice.insert() with a bare
    `TypeError: cannot unpack non-iterable NoneType object` — erpnext's
    set_pos_fields() unpacks frappe.db.get_value("Customer", ...), which is None
    when the customer row does not exist. Nothing in the traceback names the
    missing customer, so this is worth provisioning rather than diagnosing twice.

    Idempotent: returns early when the customer is already there, and never
    touches an existing one (a site may have edited its group or price list).

    default_price_list and default_currency are deliberately left empty so the
    POS Profile's selling price list and currency win instead of being
    overridden per-customer."""
    if not frappe.db.exists("DocType", "Customer"):
        return
    if frappe.db.exists("Customer", WALK_IN):
        print(f"  = Customer {WALK_IN} already present")
        return
    # Customer Group / Territory are required. Prefer the conventional defaults
    # but fall back to the roots, which every ERPNext site has.
    group = "Individual" if frappe.db.exists("Customer Group", "Individual") else "All Customer Groups"
    territory = "All Territories" if frappe.db.exists("Territory", "All Territories") else None
    doc = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": WALK_IN,
        "customer_type": "Individual",
        "customer_group": group,
        "territory": territory,
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    print(f"  + Customer {WALK_IN} (group={group}, territory={territory})")


# ─── Extra payment methods ───────────────────────────────────────────────────

def _payment_mode_account(company):
    """An account to book a non-cash mode against, for one company.

    ERPNext refuses to submit an invoice paid by a mode that has no account for
    the invoice's company, and the failure names neither the mode nor the
    company. Prefer a bank account — an aggregator settles later, the money is
    never in the drawer — and fall back to whatever Cash is already booked
    against rather than leave the mode unusable.
    """
    account = frappe.db.get_value("Company", company, "default_bank_account")
    if not account:
        account = frappe.db.get_value("Account", {"company": company, "account_type": "Bank",
                                                  "is_group": 0}, "name")
    if not account:
        account = frappe.db.get_value("Mode of Payment Account",
                                      {"parent": "Cash", "company": company}, "default_account")
    return account


def remove_payment_mode(mode="Bank Draft"):
    """Take one payment method off every till. The Mode of Payment itself, and
    every invoice that ever used it, stay.

    The mirror of add_payment_mode, and the reason it exists: the POS shows a
    button for every mode the profile lists, so a mode somebody added to a
    profile years ago and nobody ever pressed — Bank Draft, booked to the cash
    account — becomes a button on the day the till starts reading the profile.
    A cashier who has never seen it will press it. Removing the *row* is the
    smallest change that keeps the till looking as it did; the mode can be
    offered again with add_payment_mode.

    Idempotent:

        bench --site <site> execute luuvcrm.setup_doctype.remove_payment_mode \\
              --kwargs "{'mode': 'Bank Draft'}"
    """
    if not frappe.db.exists("DocType", "POS Profile"):
        return
    removed = []
    for profile in frappe.get_all("POS Profile", pluck="name"):
        pp = frappe.get_doc("POS Profile", profile)
        keep = [r for r in pp.payments if r.mode_of_payment != mode]
        if len(keep) == len(pp.payments):
            continue
        if not keep:
            print(f"  ! POS Profile {profile}: {mode} is its only method — left alone")
            continue
        pp.payments = []
        for r in keep:
            pp.append("payments", {"mode_of_payment": r.mode_of_payment,
                                   "default": r.default, "allow_in_returns": r.get("allow_in_returns")})
        pp.flags.ignore_permissions = True
        pp.save()
        removed.append(profile)
        print(f"  - POS Profile {profile} no longer offers {mode}")
    if not removed:
        print(f"  = no POS Profile offers {mode}")
    return removed


def add_payment_mode(mode="PickMe", mode_type="Bank"):
    """Add one payment method to every till, end to end.

    The POS reads its payment buttons from the POS Profile, so a mode is added
    here — not in pos.html — and the till shows it on the next load. Three
    things have to line up, and missing any one of them fails at a different
    moment: the Mode of Payment itself, an account for it per company (or the
    invoice will not submit), and a row on each POS Profile (or the button
    never appears and the shift close never reconciles it).

    Idempotent, and never edits an existing row — run it again for the next
    aggregator:

        bench --site <site> execute luuvcrm.setup_doctype.add_payment_mode
        bench --site <site> execute luuvcrm.setup_doctype.add_payment_mode \\
              --kwargs "{'mode': 'Uber Eats'}"
    """
    if not frappe.db.exists("DocType", "Mode of Payment"):
        print("  ! no Mode of Payment DocType — is erpnext installed?")
        return
    if frappe.db.exists("Mode of Payment", mode):
        print(f"  = Mode of Payment {mode} already present")
    else:
        doc = frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mode,
                              "type": mode_type, "enabled": 1})
        doc.flags.ignore_permissions = True
        doc.insert()
        print(f"  + Mode of Payment {mode} (type={mode_type})")

    profiles = frappe.get_all("POS Profile", fields=["name", "company"])

    # Book the account before touching the profiles, and off every company when
    # no till exists yet: a site is often set up in that order, and a mode left
    # accountless is one that fails at the first sale rather than here.
    companies = {p.company for p in profiles if p.company} \
        or set(frappe.get_all("Company", pluck="name"))
    doc = frappe.get_doc("Mode of Payment", mode)
    booked = {a.company for a in (doc.accounts or []) if a.default_account}
    for company in sorted(companies - booked):
        account = _payment_mode_account(company)
        if not account:
            print(f"  ! {mode}: no bank or cash account on {company} — set one by hand")
            continue
        doc.append("accounts", {"company": company, "default_account": account})
        print(f"  + {mode} account on {company}: {account}")
    doc.flags.ignore_permissions = True
    doc.save()

    if not profiles:
        print(f"  ! no POS Profile to offer {mode} on — run this again once a till exists")
        return
    for profile in profiles:
        if frappe.db.exists("POS Payment Method", {"parent": profile.name,
                                                   "mode_of_payment": mode}):
            print(f"  = POS Profile {profile.name} already offers {mode}")
            continue
        pp = frappe.get_doc("POS Profile", profile.name)
        pp.append("payments", {"mode_of_payment": mode, "default": 0})
        pp.flags.ignore_permissions = True
        pp.save()
        print(f"  + POS Profile {profile.name} now offers {mode}")


# ─── Drift report ────────────────────────────────────────────────────────────

def report_drift():
    """Read-only: what this site is missing against the definitions above.

    Writes nothing. Run it against production to see how far the live schema has
    drifted from the repo before deciding to run `execute()`:

        bench --site <site> execute luuvcrm.setup_doctype.report_drift
    """
    missing_doctypes = [n for n in EXPECTED_DOCTYPES if not frappe.db.exists("DocType", n)]
    missing_fields = [
        f"{f['dt']}.{f['fieldname']}" for f in CUSTOM_FIELDS
        if frappe.db.exists("DocType", f["dt"]) and not _has_field(f["dt"], f["fieldname"])
    ]
    absent_parents = sorted({f["dt"] for f in CUSTOM_FIELDS if not frappe.db.exists("DocType", f["dt"])})
    missing_roles = [r for r in roles.LUUVCRM_ROLES if not frappe.db.exists("Role", r)]

    missing_branch = [dt for dt in branch.BRANCHED_DOCTYPES
                      if frappe.db.exists("DocType", dt)
                      and not frappe.db.has_column(dt, branch.FIELD)]
    missing_perms = sorted(f"{dt} {p}" for dt, ps in roles.missing_docperms().items() for p in ps)
    walkable = sorted(f"{dt} {p}" for dt, ps in roles.walkable_docperms().items() for p in ps)

    for label, rows in (("DocTypes", missing_doctypes), ("custom fields", missing_fields),
                        ("roles", missing_roles), ("absent parent DocTypes", absent_parents),
                        ("branch columns", missing_branch), ("DocPerms", missing_perms),
                        ("permissions that should be revoked", walkable)):
        print(f"missing {label}: {len(rows)}")
        for r in rows:
            print(f"  - {r}")

    return {"doctypes": missing_doctypes, "custom_fields": missing_fields,
            "roles": missing_roles, "absent_parents": absent_parents,
            "branch_columns": missing_branch, "docperms": missing_perms,
            "walkable_perms": walkable}
