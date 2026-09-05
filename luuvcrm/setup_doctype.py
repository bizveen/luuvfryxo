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

NOTE: `api.setup_customer_accounts()` and `create_theme_doctype.create()` are
deliberately not called from here — both delete existing records before
recreating them. The safe equivalents are below.
"""

import frappe

from luuvcrm import branch, roles
from luuvcrm.api import EXPENSE_CATEGORIES, GUIDE_DEFAULT_PERCENT
from luuvcrm.api.errors import _install_client_errors

# ─── Shared permission blocks ────────────────────────────────────────────────

# Only System Manager is declared with the DocType. Everything else — which
# role may read, write, create or delete each of these — lives in
# `luuvcrm/roles.py` (APP_DOCPERMS / APP_DOCPERM_REVOKE) and is applied by
# `ensure_app_docperms()` at the end of execute(). One source of truth, because
# two of them is how a Cashier ended up able to answer their own approval.
MANAGER = {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}


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

def _dt(name, fields, permissions=None, **flags):
    return dict({"name": name, "fields": fields,
                 "permissions": permissions if permissions is not None else [MANAGER]}, **flags)


DOCTYPES = [
    # ── Kiosk / menu presentation ──
    _dt("Item Review", [
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "rating", "label": "Rating", "fieldtype": "Rating", "reqd": 1, "in_list_view": 1},
        {"fieldname": "comment", "label": "Comment", "fieldtype": "Small Text"},
        {"fieldname": "submitted_by", "label": "Submitted By", "fieldtype": "Data"},
    ]),

    # Add-on suggestions shown next to a menu item in the POS (managed from Desk
    # at /app/pos-item-addon). pos2 reads the enabled rows and pops a picker.
    _dt("POS Item Addon", [
        {"fieldname": "item", "label": "Menu Item", "fieldtype": "Link", "options": "Item", "reqd": 1,
         "in_list_view": 1, "in_standard_filter": 1},
        {"fieldname": "addon_item", "label": "Add-on Item", "fieldtype": "Link", "options": "Item", "reqd": 1,
         "in_list_view": 1},
        {"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check", "default": "1", "in_list_view": 1},
    ], naming_rule="Random", autoname="hash", track_changes=1),

    # Kitchen routing. Item.kitchen_group links here, so this comes before the
    # custom fields pass.
    _dt("Kitchen Group", [
        {"fieldname": "group_name", "label": "Group Name", "fieldtype": "Data", "reqd": 1, "unique": 1,
         "in_list_view": 1},
        {"fieldname": "display_order", "label": "Display Order", "fieldtype": "Int", "default": 0},
        {"fieldname": "color", "label": "Color", "fieldtype": "Data", "description": "Hex color e.g. #F59E0B"},
    ]),

    # ── WhatsApp ──
    # `api.send_order_whatsapp()` probes this with
    # `frappe.db.exists("WhatsApp Account", {})` — a *record* check, which raises
    # when the DocType itself is absent. So it has to exist even on sites that
    # never configure WhatsApp.
    _dt("WhatsApp Account", [
        {"fieldname": "account_name", "label": "Account Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
        {"fieldname": "url", "label": "API URL", "fieldtype": "Data", "default": "https://graph.facebook.com"},
        {"fieldname": "version", "label": "API Version", "fieldtype": "Data", "default": "v21.0"},
        {"fieldname": "phone_id", "label": "Phone Number ID", "fieldtype": "Data"},
        {"fieldname": "token", "label": "Access Token", "fieldtype": "Password"},
    ]),

    # ── Storefront accounts (/cus, /customer) ──
    # `address` and `area` were bolted on later by setup_customer_profile_fields;
    # they are part of the definition now.
    _dt("Customer Account", [
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
        {"fieldname": "pin", "label": "PIN", "fieldtype": "Data", "reqd": 1},
        {"fieldname": "last_login", "label": "Last Login", "fieldtype": "Datetime"},
        {"fieldname": "address", "label": "Address", "fieldtype": "Small Text"},
        {"fieldname": "area", "label": "Area", "fieldtype": "Data"},
    ]),
    _dt("Customer Token", [
        {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1},
        {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
    ]),

    # ── Loyalty ──
    _dt("Loyalty Customer", [
        {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data"},
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
        {"fieldname": "referral_code", "label": "Referral Code", "fieldtype": "Data", "unique": 1},
        {"fieldname": "referred_by", "label": "Referred By", "fieldtype": "Link", "options": "Loyalty Customer"},
        {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
        {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
        {"fieldname": "total_redeemed", "label": "Total Redeemed", "fieldtype": "Currency", "default": 0},
    ]),
    _dt("Cashback Transaction", [
        {"fieldname": "loyalty_customer", "label": "Loyalty Customer", "fieldtype": "Link",
         "options": "Loyalty Customer", "reqd": 1, "in_list_view": 1},
        {"fieldname": "type", "label": "Type", "fieldtype": "Select",
         "options": "Earn\nRedeem\nReferral Discount", "reqd": 1, "in_list_view": 1},
        {"fieldname": "level", "label": "Level", "fieldtype": "Int"},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1},
        {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]),
    _dt("Loyalty Settings", [
        {"fieldname": "level_1_percent", "label": "Level 1 (%)", "fieldtype": "Percent", "default": 5.0},
        {"fieldname": "level_2_percent", "label": "Level 2 (%)", "fieldtype": "Percent", "default": 2.0},
        {"fieldname": "level_3_percent", "label": "Level 3 (%)", "fieldtype": "Percent", "default": 1.0},
        {"fieldname": "min_redeem", "label": "Minimum Redeem (LKR)", "fieldtype": "Currency", "default": 200},
        {"fieldname": "referral_discount_percent", "label": "Referral Discount (%)", "fieldtype": "Percent",
         "default": 5.0},
        {"fieldname": "expiry_days", "label": "Expiry Days", "fieldtype": "Int", "default": 365},
    ], issingle=1),

    # ── Cash drawer ──
    # A pay-in / pay-out log linked to the shift's POS Opening Entry.
    _dt("POS Cash Movement", [
        {"fieldname": "pos_opening_entry", "label": "POS Opening Entry", "fieldtype": "Link",
         "options": "POS Opening Entry", "in_list_view": 1},
        {"fieldname": "direction", "label": "Direction", "fieldtype": "Select", "options": "in\nout",
         "reqd": 1, "in_list_view": 1},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
        {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
        {"fieldname": "cashier", "label": "Cashier", "fieldtype": "Link", "options": "User", "in_list_view": 1},
        {"fieldname": "movement_time", "label": "Time", "fieldtype": "Datetime", "in_list_view": 1},
    ], naming_rule="Random", autoname="hash", track_changes=1),

    # ── Manager approvals ──
    # `expense` joined approval_type later (setup_expenses appended it via
    # _ensure_select_option) and `manager_comment` later still (setup_wastage);
    # both are in the definition now. ensure_select_options() below catches up
    # sites created before either landed.
    _dt("POS Approval", [
        {"fieldname": "approval_type", "label": "Type", "fieldtype": "Select",
         "options": "void\nprice\nitem\nbill\ndiscount\nedit\nexpense", "in_list_view": 1},
        {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link", "options": "POS Order",
         "in_list_view": 1},
        {"fieldname": "title", "label": "Title", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "subhead", "label": "Subhead", "fieldtype": "Small Text"},
        {"fieldname": "details_json", "label": "Details JSON", "fieldtype": "Long Text"},
        {"fieldname": "flag_text", "label": "Flag", "fieldtype": "Data"},
        {"fieldname": "action", "label": "Action", "fieldtype": "Data"},
        {"fieldname": "action_payload", "label": "Action Payload", "fieldtype": "Long Text"},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "pending\napproved\nrejected",
         "default": "pending", "in_list_view": 1},
        {"fieldname": "manager_comment", "label": "Manager Comment", "fieldtype": "Small Text"},
        {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
        {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
    ], naming_rule="Random", autoname="hash", track_changes=1),

    # ── Purchases ── child table first, the parent's Table field points at it
    _dt("Purchase Bill Item", [
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
        {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "uom", "label": "UOM", "fieldtype": "Data"},
        {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "in_list_view": 1},
        {"fieldname": "price", "label": "Price", "fieldtype": "Currency", "in_list_view": 1},
        {"fieldname": "old_price", "label": "Old Price", "fieldtype": "Currency"},
    ], permissions=[], istable=1, editable_grid=1),
    _dt("Purchase Bill", [
        {"fieldname": "vendor", "label": "Vendor", "fieldtype": "Data", "in_list_view": 1, "reqd": 1},
        {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
        {"fieldname": "receipt_image", "label": "Receipt", "fieldtype": "Attach Image"},
        {"fieldname": "total", "label": "Total", "fieldtype": "Currency", "in_list_view": 1},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "pending\napproved\nrejected",
         "default": "pending", "in_list_view": 1},
        {"fieldname": "items", "label": "Items", "fieldtype": "Table", "options": "Purchase Bill Item"},
        {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
        {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
    ], naming_rule="Random", autoname="hash", track_changes=1),

    # ── Expenses ── category first, Expense.category links to it
    _dt("Expense Category", [
        {"fieldname": "category_name", "label": "Category", "fieldtype": "Data", "reqd": 1, "unique": 1,
         "in_list_view": 1},
        {"fieldname": "disabled", "label": "Disabled", "fieldtype": "Check", "default": "0", "in_list_view": 1},
    ], naming_rule="By fieldname", autoname="field:category_name",
        allow_rename=1, track_changes=1),
    _dt("Expense", [
        {"fieldname": "category", "label": "Category", "fieldtype": "Link", "options": "Expense Category",
         "reqd": 1, "in_list_view": 1},
        {"fieldname": "payee", "label": "Paid To", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
        {"fieldname": "expense_date", "label": "Date", "fieldtype": "Date", "in_list_view": 1},
        {"fieldname": "bill_ref", "label": "Bill / Account No", "fieldtype": "Data"},
        {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
        {"fieldname": "receipt_image", "label": "Receipt", "fieldtype": "Attach Image"},
        {"fieldname": "pos_profile", "label": "Outlet", "fieldtype": "Link", "options": "POS Profile"},
        {"fieldname": "pos_opening_entry", "label": "Shift", "fieldtype": "Link", "options": "POS Opening Entry"},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "pending\napproved\nrejected",
         "default": "pending", "in_list_view": 1},
        {"fieldname": "requested_by", "label": "Requested By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "requested_at", "label": "Requested At", "fieldtype": "Datetime"},
        {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
    ], naming_rule="Random", autoname="hash", track_changes=1),

    # ── Wastage ──
    _dt("Wastage", [
        {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
        {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data"},
        {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "reqd": 1, "in_list_view": 1},
        {"fieldname": "uom", "label": "Unit", "fieldtype": "Data"},
        {"fieldname": "cost", "label": "Cost", "fieldtype": "Currency", "in_list_view": 1},
        {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
        {"fieldname": "recorded_by", "label": "Recorded By", "fieldtype": "Link", "options": "User"},
    ]),

    # ── QR promo (sticker QR → register in /cus → free meal on first order) ──
    _dt("QR Promo Campaign", [
        {"fieldname": "campaign_name", "label": "Campaign Name", "fieldtype": "Data", "reqd": 1, "unique": 1,
         "in_list_view": 1},
        {"fieldname": "free_item", "label": "Free Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
        {"fieldname": "active", "label": "Active", "fieldtype": "Check", "default": "1"},
    ], naming_rule="By fieldname", autoname="field:campaign_name", track_changes=1),
    _dt("QR Promo Code", [
        {"fieldname": "code", "label": "Code", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
        {"fieldname": "campaign", "label": "Campaign", "fieldtype": "Link", "options": "QR Promo Campaign",
         "in_list_view": 1},
        {"fieldname": "batch", "label": "Batch", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "New\nRegistered\nRedeemed",
         "default": "New", "in_list_view": 1},
        {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data"},
        {"fieldname": "registered_on", "label": "Registered On", "fieldtype": "Datetime"},
        {"fieldname": "redeemed_on", "label": "Redeemed On", "fieldtype": "Datetime"},
        {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link", "options": "POS Order"},
    ], naming_rule="By fieldname", autoname="field:code", track_changes=1),

    # ── Tour guides (commission scheme) ──
    _dt("Tour Guide", [
        {"fieldname": "guide_name", "label": "Guide Name", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "whatsapp", "label": "WhatsApp", "fieldtype": "Data", "reqd": 1, "unique": 1,
         "in_list_view": 1},
        {"fieldname": "role", "label": "Role", "fieldtype": "Select", "options": "Guide\nDriver\nHelper",
         "default": "Guide", "in_list_view": 1},
        {"fieldname": "cashback_percent", "label": "Cashback %", "fieldtype": "Percent",
         "default": GUIDE_DEFAULT_PERCENT},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Invited\nActive",
         "default": "Invited", "in_list_view": 1},
        {"fieldname": "password_hash", "label": "Password Hash", "fieldtype": "Data"},
        {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
        {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
        {"fieldname": "total_paid", "label": "Total Paid", "fieldtype": "Currency", "default": 0},
        {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
        {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
    ]),
    _dt("Guide Token", [
        {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
        {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1},
        {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
    ]),
    # "Redeem" (a guide paying for their own meal out of balance) was appended
    # after this doctype first shipped; it is in the base options now.
    _dt("Guide Cashback Txn", [
        {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1,
         "in_list_view": 1},
        {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nPayout\nRedeem",
         "reqd": 1, "in_list_view": 1},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
        {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
        {"fieldname": "reference_invoice", "label": "Reference Invoice", "fieldtype": "Data"},
        {"fieldname": "bill_total", "label": "Bill Total (base)", "fieldtype": "Currency"},
        {"fieldname": "percent_used", "label": "Percent Used", "fieldtype": "Percent"},
        {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
        {"fieldname": "paid_by", "label": "Paid By", "fieldtype": "Link", "options": "User"},
    ]),
    _dt("Guide Invite Code", [
        {"fieldname": "code", "label": "Code", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
        {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "New\nUsed", "default": "New",
         "in_list_view": 1},
        {"fieldname": "used_by", "label": "Used By", "fieldtype": "Link", "options": "Tour Guide",
         "in_list_view": 1},
        {"fieldname": "used_on", "label": "Used On", "fieldtype": "Datetime"},
        {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
    ]),
    # Manager-editable global default %, beats the GUIDE_DEFAULT_PERCENT literal.
    _dt("Guide Settings", [
        {"fieldname": "default_percent", "label": "Default Cashback %", "fieldtype": "Percent",
         "default": GUIDE_DEFAULT_PERCENT},
    ], issingle=1),

    # ── Staff activity trail ──
    # Named `LF Activity Log` because Frappe core already owns `Activity Log`.
    # Kept in sync with luuvcrm/activity.py, which reads and writes these fields.
    _dt("LF Activity Log", [
        {"fieldname": "app", "label": "App", "fieldtype": "Data", "search_index": 1, "in_list_view": 1},
        {"fieldname": "action", "label": "Action", "fieldtype": "Data", "search_index": 1, "in_list_view": 1},
        {"fieldname": "label", "label": "Label", "fieldtype": "Data", "in_list_view": 1},
        {"fieldname": "source", "label": "Source", "fieldtype": "Select", "options": "server\nclient\nrequest",
         "default": "server"},
        {"fieldname": "user", "label": "User", "fieldtype": "Data", "search_index": 1},
        {"fieldname": "user_full", "label": "User Name", "fieldtype": "Data"},
        # What it happened to
        {"fieldname": "ref_doctype", "label": "Ref DocType", "fieldtype": "Data"},
        {"fieldname": "ref_name", "label": "Ref Name", "fieldtype": "Data", "search_index": 1},
        {"fieldname": "bill_no", "label": "Bill", "fieldtype": "Data", "search_index": 1},
        {"fieldname": "table", "label": "Table", "fieldtype": "Data"},
        {"fieldname": "item", "label": "Item", "fieldtype": "Data"},
        {"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data"},
        {"fieldname": "qty", "label": "Qty", "fieldtype": "Float", "precision": "2"},
        # Money
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Float", "precision": "2", "in_list_view": 1},
        {"fieldname": "total_before", "label": "Total Before", "fieldtype": "Float", "precision": "2"},
        {"fieldname": "total_after", "label": "Total After", "fieldtype": "Float", "precision": "2"},
        # The point of the whole thing
        {"fieldname": "flag", "label": "Flagged", "fieldtype": "Check", "default": "0", "search_index": 1,
         "in_list_view": 1},
        {"fieldname": "flag_reason", "label": "Flag Reason", "fieldtype": "Data"},
        {"fieldname": "flag_note", "label": "Flag Note", "fieldtype": "Small Text"},
        {"fieldname": "reviewed", "label": "Reviewed", "fieldtype": "Check", "default": "0"},
        {"fieldname": "reviewed_by", "label": "Reviewed By", "fieldtype": "Data"},
        {"fieldname": "reviewed_at", "label": "Reviewed At", "fieldtype": "Datetime"},
        # Context
        {"fieldname": "detail", "label": "Detail", "fieldtype": "Long Text"},
        {"fieldname": "page", "label": "Page", "fieldtype": "Data"},
        {"fieldname": "ip", "label": "IP", "fieldtype": "Data"},
        {"fieldname": "device", "label": "Device", "fieldtype": "Small Text"},
        {"fieldname": "session_id", "label": "Session", "fieldtype": "Data", "search_index": 1},
        {"fieldname": "seq", "label": "Seq", "fieldtype": "Int"},
        {"fieldname": "client_ts", "label": "Client Time", "fieldtype": "Datetime"},
        {"fieldname": "ok", "label": "OK", "fieldtype": "Check", "default": "1"},
        {"fieldname": "err", "label": "Error", "fieldtype": "Small Text"},
        {"fieldname": "ms", "label": "Duration ms", "fieldtype": "Int"},
    ], naming_rule="Random", autoname="hash"),
]


# ─── Custom fields ───────────────────────────────────────────────────────────
# Fields the Python/JS code reads or writes but that no DocType JSON defines.
# Applied after DOCTYPES, so the fields hanging off Purchase Bill and the
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

    # Zeloura storefront item attributes (JSON blobs parsed by zeloura_api)
    {"dt": "Item", "fieldname": "z_sizes", "label": "Zeloura Sizes (JSON)", "fieldtype": "Long Text"},
    {"dt": "Item", "fieldname": "z_colors", "label": "Zeloura Colors (JSON)", "fieldtype": "Long Text"},
    {"dt": "Item", "fieldname": "z_images", "label": "Zeloura Images (JSON)", "fieldtype": "Long Text"},
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

    _step("settings singles", ensure_singles)
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
    created = 0
    for spec in DOCTYPES:
        spec = dict(spec)
        name = spec.pop("name")
        if frappe.db.exists("DocType", name):
            continue
        frappe.get_doc(dict({
            "doctype": "DocType",
            "name": name,
            "module": "Zeloura",
            "custom": 1,
        }, **spec)).insert(ignore_permissions=True)
        print(f"  + DocType {name}")
        created += 1
    print(f"DocTypes: {created} created, {len(DOCTYPES) - created} already present")


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


# ─── Drift report ────────────────────────────────────────────────────────────

def report_drift():
    """Read-only: what this site is missing against the definitions above.

    Writes nothing. Run it against production to see how far the live schema has
    drifted from the repo before deciding to run `execute()`:

        bench --site <site> execute luuvcrm.setup_doctype.report_drift
    """
    missing_doctypes = [d["name"] for d in DOCTYPES if not frappe.db.exists("DocType", d["name"])]
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
