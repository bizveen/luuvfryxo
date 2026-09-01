"""Idempotent provisioning for the app's custom fields and runtime DocTypes.

Runs unattended on every `bench migrate` (see `after_migrate` in hooks.py), so
every helper in here sticks to three rules:

  * create-only  — nothing here deletes or rewrites data that already exists
  * guarded      — a field/DocType already present (standard *or* custom) is skipped
  * non-fatal    — a failure is logged and skipped, never allowed to abort a migrate

That last rule matters most: an exception raised from an after_migrate hook fails
the whole migrate, which on a live site means a broken deploy.

NOTE: `api.setup_customer_accounts()` and `create_theme_doctype.create()` are
deliberately NOT called from here — both delete existing records before
recreating. The safe equivalents live below.
"""

import frappe

# ─── Custom fields ───────────────────────────────────────────────────────────
# Fields the Python/JS code reads or writes but that no DocType JSON defines.
# Order matters only where `insert_after` points at another row in this list.
CUSTOM_FIELDS = [
    # Kiosk / menu presentation
    {"dt": "Item", "fieldname": "speciality_tags", "label": "Speciality Tags", "fieldtype": "Small Text",
     "description": "Comma-separated: Chef's Special, Most Popular, Spicy, Vegan, Gluten-Free"},
    {"dt": "Item", "fieldname": "youtube_url", "label": "YouTube Video URL", "fieldtype": "Data"},
    {"dt": "Item", "fieldname": "prep_time", "label": "Preparation Time (mins)", "fieldtype": "Int"},
    {"dt": "Item", "fieldname": "kitchen_group", "label": "Kitchen Group", "fieldtype": "Data"},

    # Restaurant settings
    {"dt": "Restaurant", "fieldname": "service_charge", "label": "Service Charge (%)", "fieldtype": "Percent",
     "description": "Service charge percentage added to all orders", "insert_after": "phone"},
    {"dt": "Restaurant", "fieldname": "guide_commission_percent", "label": "Guide Commission (%)",
     "fieldtype": "Percent", "description": "Guide commission percentage"},

    # POS Order header
    {"dt": "POS Order", "fieldname": "order_type", "label": "Order Type", "fieldtype": "Data",
     "default": "Dine-In"},
    {"dt": "POS Order", "fieldname": "discount_amount", "label": "Discount Amount", "fieldtype": "Currency",
     "insert_after": "service_charge_amount", "read_only": 1,
     "description": "Set by apply_discount(); not entered by hand"},
    {"dt": "POS Order", "fieldname": "discount_note", "label": "Discount Note", "fieldtype": "Data",
     "insert_after": "discount_amount", "read_only": 1},
    {"dt": "POS Order", "fieldname": "pos_opening_entry", "label": "POS Opening Entry", "fieldtype": "Link",
     "options": "POS Opening Entry", "insert_after": "pos_invoice", "read_only": 1,
     "description": "Shift this order was rung up in"},

    # POS Order — WhatsApp notification state
    {"dt": "POS Order", "fieldname": "whatsapp_status", "label": "WhatsApp Status", "fieldtype": "Data",
     "read_only": 1},
    {"dt": "POS Order", "fieldname": "wa_message_id", "label": "WhatsApp Message ID", "fieldtype": "Data",
     "read_only": 1, "no_copy": 1},

    # POS Order lines
    {"dt": "POS Order Item", "fieldname": "takeaway", "label": "Takeaway", "fieldtype": "Check",
     "default": "0", "insert_after": "rate"},
    {"dt": "POS Order Item", "fieldname": "sent_qty", "label": "Sent Qty", "fieldtype": "Int",
     "default": "0", "insert_after": "takeaway"},
    {"dt": "POS Order Item", "fieldname": "paid_qty", "label": "Paid Qty", "fieldtype": "Int",
     "default": "0", "insert_after": "sent_qty",
     "description": "How many of this line have already been settled (bill splitting)"},
    {"dt": "POS Order Item", "fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},

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

    # Invoice lines carry the takeaway flag through to the printed bill
    {"dt": "POS Invoice Item", "fieldname": "takeaway", "label": "Takeaway", "fieldtype": "Check",
     "default": "0", "insert_after": "rate"},

    # Zeloura storefront item attributes (JSON blobs parsed by zeloura_api)
    {"dt": "Item", "fieldname": "z_sizes", "label": "Zeloura Sizes (JSON)", "fieldtype": "Long Text"},
    {"dt": "Item", "fieldname": "z_colors", "label": "Zeloura Colors (JSON)", "fieldtype": "Long Text"},
    {"dt": "Item", "fieldname": "z_images", "label": "Zeloura Images (JSON)", "fieldtype": "Long Text"},
]

_CF_KEYS = ("options", "description", "default", "insert_after", "read_only", "no_copy",
            "in_list_view", "reqd", "unique", "hidden", "precision")


def execute():
    _step("custom fields", add_custom_fields)
    _step("Item Review", create_item_review_doctype)
    _step("POS Item Addon", ensure_pos_item_addon)
    _step("WhatsApp Account", ensure_whatsapp_account)
    _step("customer accounts", ensure_customer_accounts)
    _step("Walk In customer", ensure_walk_in_customer)
    _step("POS runtime DocTypes", ensure_pos_doctypes)
    frappe.db.commit()
    print("luuvcrm setup complete")


# ─── Safety helpers ──────────────────────────────────────────────────────────

def _step(label, fn):
    """Run one provisioning step; log and continue if it blows up.

    Each step gets its own savepoint so a half-applied failure cannot poison the
    steps that follow (or the migrate's own transaction)."""
    try:
        frappe.db.savepoint("luuvcrm_setup")
        fn()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point="luuvcrm_setup")
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


def _ensure_doctype(name, module="Zeloura", fields=None, permissions=None, **flags):
    """Create a custom DocType only when it is missing. Never touches an existing one."""
    if frappe.db.exists("DocType", name):
        return False
    doc = frappe.get_doc(dict({
        "doctype": "DocType",
        "name": name,
        "module": module,
        "custom": 1,
        "fields": fields or [],
        "permissions": permissions or [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        ],
    }, **flags))
    doc.insert(ignore_permissions=True)
    print(f"  + created DocType {name}")
    return True


# ─── Custom fields ───────────────────────────────────────────────────────────

def add_custom_fields():
    added = 0
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
        frappe.get_doc(cf).insert(ignore_permissions=True)
        print(f"  + {dt}.{fieldname}")
        added += 1
    print(f"custom fields: {added} added, {len(CUSTOM_FIELDS) - added} already present or N/A")


# The anonymous POS customer. Must match the literal used in api.py
# (place_order / process_payment / pay_split / kiosk_place_order).
WALK_IN = "Walk In"


# ─── DocTypes ────────────────────────────────────────────────────────────────

def create_item_review_doctype():
    _ensure_doctype("Item Review", fields=[
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "rating", "label": "Rating", "fieldtype": "Rating", "reqd": 1, "in_list_view": 1},
        {"fieldname": "comment", "label": "Comment", "fieldtype": "Small Text"},
        {"fieldname": "submitted_by", "label": "Submitted By", "fieldtype": "Data"},
    ], permissions=[
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        {"role": "Guest", "read": 1, "write": 1, "create": 1},
    ])


def ensure_pos_item_addon():
    """Add-on suggestions shown next to a menu item in the POS (managed from Desk)."""
    _ensure_doctype("POS Item Addon", fields=[
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "addon_item", "label": "Add-on Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check", "default": "1", "in_list_view": 1},
    ])


def ensure_whatsapp_account():
    """Cloud API credentials for order notifications.

    `api.send_order_whatsapp()` probes this with `frappe.db.exists("WhatsApp Account", {})`
    — a *record* check, which raises when the DocType itself is absent, so the
    DocType has to exist even on sites that never configure WhatsApp."""
    _ensure_doctype("WhatsApp Account", fields=[
        {"fieldname": "account_name", "label": "Account Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "url", "label": "API URL", "fieldtype": "Data", "default": "https://graph.facebook.com"},
        {"fieldname": "version", "label": "API Version", "fieldtype": "Data", "default": "v21.0"},
        {"fieldname": "phone_id", "label": "Phone Number ID", "fieldtype": "Data"},
        {"fieldname": "token", "label": "Access Token", "fieldtype": "Password"},
    ])


def ensure_customer_accounts():
    """Storefront login accounts + session tokens.

    Deliberately not delegated to `api.setup_customer_accounts()`, which drops and
    recreates both DocTypes — that would wipe every saved PIN on a live site."""
    _ensure_doctype("Customer Account", fields=[
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
        {"fieldname": "pin", "label": "PIN", "fieldtype": "Data", "reqd": 1},
        {"fieldname": "last_login", "label": "Last Login", "fieldtype": "Datetime"},
    ])
    _ensure_doctype("Customer Token", fields=[
        {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1},
        {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
    ])


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


def ensure_pos_doctypes():
    """Delegate to the existing setup_* endpoints in api.py.

    Each already returns early when its DocType is present, so calling them on a
    provisioned site is a no-op; they are only listed here so a fresh install (or
    a site where the endpoint was never hit by hand) ends up in the same state."""
    from luuvcrm import api

    for label, creates, fn in (
        ("Kitchen Group", "Kitchen Group", api.setup_kitchen_groups),
        ("Loyalty", "Loyalty Customer", api.setup_loyalty),
        ("POS Approval", "POS Approval", api.setup_pos_approval),
        ("Cash Movement", "POS Cash Movement", api.setup_cash_movement),
        ("Purchase Bill", "Purchase Bill", api.setup_purchase_bill),
    ):
        if frappe.db.exists("DocType", creates):
            continue
        fn()
        print(f"  + provisioned {label}")
