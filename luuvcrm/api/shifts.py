"""The till: POS profile and invoice plumbing, opening and closing a shift, the cash drawer.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import json
from frappe.utils import now_datetime
from luuvcrm import activity, branch, roles


# The role model lives in luuvcrm/roles.py. Guards are called as
# roles.require_pos() / roles.require_manager() so every call site says where
# the rule comes from.

def _resolve_pos_profile_name(requested=""):
    """A POS Profile this session is permitted to operate."""
    if requested:
        branch.require_access("POS Profile", requested)
        return requested
    profiles = branch.accessible_pos_profiles()
    if not profiles:
        frappe.throw(_("No POS Profile is configured for your restaurant"),
                     frappe.PermissionError)
    return profiles[0]
def _link_invoice_to_order(invoice_name, order_name):
    """Set the reverse invoice -> POS Order link (POS Invoice or Sales Invoice)."""
    dt = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
    if frappe.db.has_column(dt, "pos_order"):
        frappe.db.set_value(dt, invoice_name, "pos_order", order_name)
def _active_pos_profile_name(requested=""):
    """POS Profile of the session user's OPEN shift (so POS Invoices match the opening
    entry for native consolidation), else requested, else first profile.

    A multi-branch owner may have an old open shift in another restaurant.  It
    must not override the restaurant currently selected in the shared header.
    Only consider shifts whose profiles are accessible in that selected branch.
    """
    profiles = branch.accessible_pos_profiles()
    if not profiles:
        frappe.throw(_("No POS Profile is configured for your restaurant"),
                     frappe.PermissionError)
    prof = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "docstatus": 1, "user": frappe.session.user,
         "pos_profile": ["in", profiles]}, "pos_profile",
        order_by="period_start_date desc")
    return prof or _resolve_pos_profile_name(requested)


def _active_open_shift_name(user=None, fallback_to_branch=False):
    """An open shift belonging to the restaurant selected in the app header."""
    user = user or frappe.session.user
    profiles = branch.accessible_pos_profiles(user)
    if not profiles:
        return None
    filters = {"docstatus": 1, "status": "Open",
               "pos_profile": ["in", profiles]}
    filters["user"] = user
    name = frappe.db.get_value("POS Opening Entry", filters, "name",
                               order_by="creation desc")
    if name or not fallback_to_branch:
        return name
    filters.pop("user", None)
    return frappe.db.get_value("POS Opening Entry", filters, "name",
                               order_by="creation desc")
def _invoice_doctype_for(pos_profile_name):
    """POS sales always become native POS Invoices (open/close entries + consolidation).
    An open shift is guaranteed by _ensure_open_shift() before any invoice is created."""
    return "POS Invoice"


def _invoice_branch_fields(pos_profile):
    """Accounting dimensions that make a POS Invoice belong to its outlet.

    ERPNext does not reliably copy ``POS Profile.cost_center`` when these API
    endpoints construct a POS Invoice directly.  Reports separate restaurants
    by that native field, so leaving it blank makes a genuine sale disappear
    as soon as the manager switches from an all-outlet view to one branch.
    Prefer the Restaurant's configured centre and retain the profile's native
    centre as a fallback for older/single-branch setups.
    """
    restaurant = branch._restaurant_for_profile(pos_profile.name)
    centre = branch.cost_center(restaurant) if restaurant else None
    centre = centre or pos_profile.get("cost_center")
    return {"cost_center": centre} if centre else {}


def _ensure_open_shift(pos_profile_name):
    """Guarantee the session user has an open shift for the profile; auto-open one
    (0 opening per payment mode) if not. POS Invoice creation requires an open POS
    Opening Entry, so this keeps every cashier sale on the native POS pipeline."""
    branch.require_access("POS Profile", pos_profile_name)
    user = frappe.session.user
    existing = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "docstatus": 1, "user": user, "pos_profile": pos_profile_name}, "name")
    if existing:
        return existing
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    opening = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "pos_profile": pos_profile_name,
        "company": pos_profile.company,
        "period_start_date": now_datetime(),
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "user": user,
        "balance_details": [{"mode_of_payment": m, "opening_amount": 0}
                            for m in _profile_payment_modes(pos_profile_name)],
    })
    opening.flags.ignore_permissions = True
    opening.insert()
    opening.submit()
    return opening.name
def _shift_unconsolidated_invoices(user, pos_profile):
    """Every submitted, UNCONSOLIDATED POS Invoice for this cashier+profile — i.e. the open
    shift's sales. We attribute by "unconsolidated" rather than ERPNext's [period_start, now]
    time window: a window silently drops any bill whose posting timestamp falls outside it (a
    sale paid right at/just before a previous close, clock skew, offline replay) which makes the
    close show Rs 0 / undercount. Closed shifts already consolidated theirs, so this is exactly
    the current shift's set."""
    return frappe.db.sql("""
        select name, grand_total
        from `tabPOS Invoice`
        where owner = %s and docstatus = 1 and pos_profile = %s
          and ifnull(consolidated_invoice, '') = ''
        order by timestamp(posting_date, posting_time)
    """, (user, pos_profile), as_dict=True)
def _get_invoice_doc(invoice_name):
    """Load an order's invoice regardless of whether it is a POS Invoice or Sales Invoice."""
    dt = "POS Invoice" if frappe.db.exists("POS Invoice", invoice_name) else "Sales Invoice"
    return frappe.get_doc(dt, invoice_name)
def _service_charge_account(company):
    """Income account the restaurant service charge posts to.
    The service charge is a charge/tax on the bill, not a sellable item, so it goes
    into the invoice's Sales Taxes and Charges table against an income account."""
    abbr = frappe.get_cached_value("Company", company, "abbr")
    for nm in (f"Service Charge - {abbr}", f"Service - {abbr}"):
        if frappe.db.exists("Account", nm):
            return nm
    return frappe.get_cached_value("Company", company, "default_income_account")
def _append_service_charge_tax(invoice, sc_amount):
    """Add the service charge as an Actual Sales Taxes and Charges row (not a line item).
    sc_amount is the pre-computed dine-in-only amount, so it's added verbatim."""
    invoice.append("taxes", {
        "charge_type": "Actual",
        "account_head": _service_charge_account(invoice.company),
        "description": "Service Charge",
        "tax_amount": sc_amount,
        "category": "Total",
        "add_deduct_tax": "Add",
    })
def _ensure_order_type_field():
    if not frappe.db.exists("Custom Field", {"dt": "POS Order", "fieldname": "order_type"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "POS Order",
            "fieldname": "order_type",
            "label": "Order Type",
            "fieldtype": "Select",
            "options": "Dine In\nTake Away\nDelivery",
            "insert_after": "order_source",
        })
        cf.flags.ignore_permissions = True
        cf.insert()
# ─── POS Shift (ERPNext POS Opening/Closing Entry) ──────

def _profile_payment_modes(pos_profile_name):
    """Ordered mode-of-payment names configured on a POS Profile (Cash first if present)."""
    modes = frappe.get_all("POS Payment Method",
        filters={"parent": pos_profile_name}, fields=["mode_of_payment"], order_by="idx")
    names = [m.mode_of_payment for m in modes if m.mode_of_payment]
    return names or ["Cash"]
@frappe.whitelist(methods=["POST"])
def pos_open_shift():
    roles.require_pos()
    data = frappe.local.form_dict
    pos_profile_name = _resolve_pos_profile_name(data.get("pos_profile", ""))

    # Use the same active-restaurant scope as get_pos_shift()/pos_close_shift().
    # Otherwise an owner with an old open till at another outlet sees this
    # error while the current outlet correctly shows no shift — leaving no
    # Close shift button and no way to open this till.
    existing = _active_open_shift_name()
    if existing:
        frappe.throw(_("A shift is already open. Close it first."))

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)

    # Per-mode opening amounts (native). Accept posted balance_details, else fall back to
    # the profile's payment methods at 0 (legacy single opening_balance honoured for Cash).
    posted = data.get("balance_details")
    amounts = {}
    if posted:
        try:
            for row in json.loads(posted):
                if row.get("mode_of_payment"):
                    amounts[row["mode_of_payment"]] = float(row.get("opening_amount") or 0)
        except (ValueError, TypeError):
            amounts = {}
    if not amounts and data.get("opening_balance") is not None:
        amounts["Cash"] = float(data.get("opening_balance") or 0)

    balance_details = [
        {"mode_of_payment": mode, "opening_amount": amounts.get(mode, 0)}
        for mode in _profile_payment_modes(pos_profile_name)
    ]

    opening = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "pos_profile": pos_profile_name,
        "company": pos_profile.company,
        "period_start_date": now_datetime(),
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "user": frappe.session.user,
        "balance_details": balance_details,
    })
    opening.insert()
    opening.submit()

    opening_amt = sum(float(d["opening_amount"]) for d in balance_details)

    activity.log_activity(
        "shift.open", label="Shift opened, float Rs %.0f" % opening_amt,
        ref_doctype="POS Opening Entry", ref_name=opening.name,
        amount=opening_amt, total_after=opening_amt,
        detail={"pos_profile": pos_profile_name, "opening": balance_details})

    return {
        "shift": {
            "status": "open",
            "name": opening.name,
            "pos_profile": pos_profile_name,
            "opening_balance": opening_amt,
            "opening_details": balance_details,
            "total_sales": 0,
            "order_count": 0,
            "payment_breakdown": {},
            "period_start": str(opening.period_start_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }
def _consolidate_shift_inline(closing):
    """Merge a POS Closing Entry's POS Invoices into Sales Invoice(s) RIGHT NOW, in this process.

    ERPNext's consolidate_pos_invoices() enqueues this onto the background queue whenever a shift
    has >= 10 POS Invoices. This bench's queue workers can't import an installed app, so that queued
    job dies — the closing is left status="Queued"/"Failed" and the opening stuck "Open" (the cashier
    can then neither open a new shift nor log out). Calling create_merge_logs() directly in the
    backend, where imports work, does the exact same consolidation reliably. Idempotent-ish: skip
    when the closing already consolidated (status "Submitted")."""
    from erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log import (
        get_invoice_customer_map,
        create_merge_logs,
    )
    invoices = closing.get("pos_transactions") or []
    if not invoices:
        # Nothing to merge — just flip the opening to Closed.
        closing.update_opening_entry()
        closing.set_status(update=True, status="Submitted")
        return
    invoice_by_customer = get_invoice_customer_map(invoices)
    create_merge_logs(invoice_by_customer, closing)  # creates Sales Invoice(s) + marks opening Closed
@frappe.whitelist(methods=["POST"])
def pos_close_shift():
    roles.require_pos()
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    data = frappe.local.form_dict

    # Per-mode counted amounts (native). Accept posted closing_details, else fall back to the
    # legacy single closing_balance (applied to Cash).
    counted = {}
    posted = data.get("closing_details")
    if posted:
        try:
            for row in json.loads(posted):
                if row.get("mode_of_payment"):
                    counted[row["mode_of_payment"]] = float(row.get("closing_amount") or 0)
        except (ValueError, TypeError):
            counted = {}
    if not counted and data.get("closing_balance") is not None:
        counted["Cash"] = float(data.get("closing_balance") or 0)

    opening_name = _active_open_shift_name()
    if not opening_name:
        frappe.throw(_("No open shift found"))

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    # ERPNext gathers the closing's invoices over [period_start_date, now]. Widen the start (in
    # memory only) to the earliest unconsolidated bill for this cashier+profile so a sale paid
    # at/just before this shift opened is still captured — otherwise it's silently dropped and the
    # close shows Rs 0 / undercounts. Restore the field right after (make_closing_entry only reads
    # it) so the later opening.save() keeps the true shift start.
    _true_start = opening.period_start_date
    _earliest = frappe.db.sql("""
        select min(timestamp(posting_date, posting_time)) from `tabPOS Invoice`
        where owner = %s and docstatus = 1 and pos_profile = %s and ifnull(consolidated_invoice, '') = ''
    """, (opening.user, opening.pos_profile))[0][0]
    if _earliest and str(_earliest) < str(_true_start):
        opening.period_start_date = _earliest

    # Native: gather this shift's submitted POS Invoices into a POS Closing Entry
    # (pos_transactions + taxes + payment reconciliation built by ERPNext).
    closing = make_closing_entry_from_opening(opening)
    opening.period_start_date = _true_start  # restore so update_opening_entry doesn't persist the widened start

    # Apply the cashier's counted amount per mode; trust expected where none counted.
    payment_breakdown = {}
    for row in closing.payment_reconciliation:
        expected = float(row.expected_amount or 0)
        opening_amt_row = float(row.opening_amount or 0)
        row.closing_amount = counted.get(row.mode_of_payment, expected)
        row.difference = float(row.closing_amount) - expected
        payment_breakdown[row.mode_of_payment] = expected - opening_amt_row

    closing.flags.ignore_permissions = True
    closing.insert()
    closing.submit()  # docstatus=1; ERPNext consolidates inline for <10 invoices, else enqueues
    closing.reload()

    # For a big shift (>= 10 invoices) ERPNext only QUEUED the consolidation — and this bench's
    # queue worker can't run it, which is exactly what left shifts stuck "Open". Finish it here.
    consolidated = True
    if closing.status == "Queued":
        # Flip the opening to Closed up front so the cashier is unwedged even if the heavy merge
        # below is slow/fails (they can always open a fresh shift or log out). Committed first so a
        # later rollback of a failed merge can't reopen the shift.
        opening.db_set("status", "Closed")
        opening.db_set("pos_closing_entry", closing.name)
        frappe.db.commit()
        try:
            _consolidate_shift_inline(closing)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "POS shift consolidation failed")
            consolidated = False

    opening_amt = sum(float(d.opening_amount or 0) for d in opening.balance_details)
    total_sales = float(closing.grand_total or 0)
    order_count = len(closing.pos_transactions or [])

    # The drawer count is the one number in this app nobody else can check: the
    # cashier types it, and expected comes from the invoices. The difference is
    # what the flag exists for, and the largest one across modes wins the row.
    worst = max(closing.payment_reconciliation,
                key=lambda r: abs(float(r.difference or 0)), default=None)
    flag, note = activity.variance_flag(
        float(worst.difference or 0) if worst else 0,
        worst.mode_of_payment if worst else "Cash")
    activity.log_activity(
        "shift.close",
        label="Shift closed, %d bills, Rs %.0f" % (order_count, total_sales),
        ref_doctype="POS Closing Entry", ref_name=closing.name,
        amount=total_sales, total_before=opening_amt,
        total_after=opening_amt + total_sales,
        flag_reason=flag, flag_note=note,
        detail={"opening_entry": opening_name, "orders": order_count,
                "consolidated": consolidated,
                "counted": {r.mode_of_payment: float(r.closing_amount or 0)
                            for r in closing.payment_reconciliation},
                "expected": {r.mode_of_payment: float(r.expected_amount or 0)
                             for r in closing.payment_reconciliation},
                "difference": {r.mode_of_payment: float(r.difference or 0)
                               for r in closing.payment_reconciliation}})

    return {
        "shift": {
            "status": "closed",
            "name": closing.name,
            "opening_name": opening_name,
            "opening_balance": opening_amt,
            "total_sales": total_sales,
            "order_count": order_count,
            "consolidated": consolidated,
            "payment_breakdown": payment_breakdown,
            "payment_reconciliation": [{
                "mode_of_payment": r.mode_of_payment,
                "opening_amount": float(r.opening_amount or 0),
                "expected_amount": float(r.expected_amount or 0),
                "closing_amount": float(r.closing_amount or 0),
                "difference": float(r.difference or 0),
            } for r in closing.payment_reconciliation],
            "period_start": str(closing.period_start_date),
            "period_end": str(closing.period_end_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }
@frappe.whitelist()
def get_shift_closing_data():
    """Return shift closing data for re-printing summary."""
    roles.require_pos()
    closing_name = frappe.local.form_dict.get("closing_name", "")
    if not closing_name:
        frappe.throw(_("Closing name is required"))
    try:
        closing = frappe.get_doc("POS Closing Entry", closing_name)
    except frappe.DoesNotExistError:
        frappe.throw(_("Closing entry not found"))

    opening_amt = 0
    for d in (closing.get("payment_reconciliation") or []):
        opening_amt += float(d.opening_amount)

    payment_breakdown = {}
    payment_reconciliation = []
    for d in (closing.get("payment_reconciliation") or []):
        # Sales per mode = expected - opening (closing_amount is the cashier's COUNT,
        # which drifts from sales whenever the drawer is over/short).
        payment_breakdown[d.mode_of_payment] = float(d.expected_amount) - float(d.opening_amount)
        payment_reconciliation.append({
            "mode_of_payment": d.mode_of_payment,
            "opening_amount": float(d.opening_amount),
            "expected_amount": float(d.expected_amount),
            "closing_amount": float(d.closing_amount),
            "difference": float(d.difference)
        })

    return {
        "shift": {
            "status": "closed",
            "name": closing.name,
            "opening_balance": opening_amt,
            "total_sales": closing.grand_total,
            "net_total": closing.net_total,
            "order_count": 0,
            "payment_breakdown": payment_breakdown,
            "payment_reconciliation": payment_reconciliation,
            "period_start": str(closing.period_start_date),
            "period_end": str(closing.period_end_date),
            "cashier": closing.user
        }
    }
@frappe.whitelist(methods=["POST"])
def recover_stuck_shift(opening=None):
    """Force-finish a shift left stuck "Open" because its consolidation failed/queued (e.g. a big
    shift whose merge was sent to a dead queue worker). Cancels any half-done Failed/Queued closings
    for the opening, builds a fresh closing from its still-unconsolidated invoices, consolidates
    inline, and marks the opening Closed. Safe to call on any of the user's stuck shifts (or pass an
    explicit opening name for a manager to recover a cashier's shift)."""
    roles.require_pos()
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    opening_name = opening or _active_open_shift_name()
    if not opening_name:
        frappe.throw(_("No stuck shift found"))
    branch.require_access("POS Profile",
                          frappe.db.get_value("POS Opening Entry", opening_name, "pos_profile"))
    opening_doc = frappe.get_doc("POS Opening Entry", opening_name)

    # Drop earlier half-done closings (Failed/Queued) — nothing was consolidated, so cancelling is
    # safe and lets us build one clean closing over the current invoices. Best-effort.
    for c in frappe.get_all("POS Closing Entry",
            filters={"pos_opening_entry": opening_name, "docstatus": 1,
                     "status": ["in", ["Failed", "Queued"]]}, pluck="name"):
        try:
            cdoc = frappe.get_doc("POS Closing Entry", c)
            cdoc.flags.ignore_permissions = True
            cdoc.cancel()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "recover_stuck_shift: cancel old closing")
    frappe.db.commit()

    closing = make_closing_entry_from_opening(opening_doc)
    for row in closing.payment_reconciliation:
        # On recovery we trust the system-expected amounts (the cashier's count was lost with the
        # failed close); a manager can adjust later if needed.
        row.closing_amount = float(row.expected_amount or 0)
        row.difference = 0
    closing.flags.ignore_permissions = True
    closing.insert()
    closing.submit()
    closing.reload()

    consolidated = closing.status != "Queued"
    if closing.status == "Queued":
        opening_doc.db_set("status", "Closed")
        opening_doc.db_set("pos_closing_entry", closing.name)
        frappe.db.commit()
        try:
            _consolidate_shift_inline(closing)
            frappe.db.commit()
            consolidated = True
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "recover_stuck_shift: consolidation failed")
            consolidated = False

    return {
        "recovered": True,
        "opening": opening_name,
        "closing": closing.name,
        "invoices": len(closing.pos_transactions or []),
        "consolidated": consolidated,
    }
@frappe.whitelist()
def get_pos_shift():
    roles.require_pos()
    opening_name = _active_open_shift_name()
    if not opening_name:
        return {"shift": None}

    opening = frappe.get_doc("POS Opening Entry", opening_name)

    opening_amt = 0
    opening_details = []
    for d in opening.balance_details:
        opening_amt += float(d.opening_amount)
        opening_details.append({"mode_of_payment": d.mode_of_payment,
                                "opening_amount": float(d.opening_amount or 0)})

    # Live totals from this shift's UNCONSOLIDATED POS Invoices (attributed by user+profile, not a
    # fragile [period_start, now] window) so the close modal shows every bill — never Rs 0 while
    # sales exist. payment_breakdown sums each invoice's payment rows.
    invoices = _shift_unconsolidated_invoices(opening.user, opening.pos_profile)
    total_sales = sum(float(i.get("grand_total") or 0) for i in invoices)
    order_count = len(invoices)

    payment_breakdown = {}
    for p in frappe.db.sql("""
        select sp.mode_of_payment as mode, sum(sp.amount) as amt
        from `tabSales Invoice Payment` sp
        join `tabPOS Invoice` pi on sp.parent = pi.name
        where pi.owner = %s and pi.docstatus = 1 and pi.pos_profile = %s
          and ifnull(pi.consolidated_invoice, '') = ''
        group by sp.mode_of_payment
    """, (opening.user, opening.pos_profile), as_dict=True):
        if p.mode:
            payment_breakdown[p.mode] = float(p.amt or 0)

    return {
        "shift": {
            "status": "open",
            "name": opening_name,
            "pos_profile": opening.pos_profile,
            "opening_balance": opening_amt,
            "opening_details": opening_details,
            "total_sales": total_sales,
            "order_count": order_count,
            "payment_breakdown": payment_breakdown,
            "period_start": str(opening.period_start_date),
            "cashier": frappe.get_value("User", opening.user, "full_name") or opening.user
        }
    }
# ─── POS Cash Movements (drawer pay-in / pay-out) ────────
# ERPNext has no native mid-shift cash in/out; the Frappe-default way to persist a new
# record type is a DocType. POS Cash Movement is a per-shift drawer log (not GL-integrated).

def _open_shift_name(user=None):
    """The user's active-restaurant shift, else one active-restaurant till."""
    return _active_open_shift_name(user, fallback_to_branch=True)
def _install_cash_movement():
    """Create the `POS Cash Movement` Custom DocType (idempotent; custom:1 => no bench migrate).
    A small drawer pay-in / pay-out log linked to the shift's POS Opening Entry."""
    if frappe.db.exists("DocType", "POS Cash Movement"):
        return {"created": False, "exists": True}

    # Cashier role is used by the POS; ensure it exists (mirrors setup_pos_shift_perms).
    if not frappe.db.exists("Role", "Cashier"):
        frappe.get_doc({"doctype": "Role", "role_name": "Cashier", "desk_access": 1}).insert(
            ignore_permissions=True
        )

    dt = frappe.get_doc({
        "doctype": "DocType",
        "name": "POS Cash Movement",
        "module": "Zeloura",
        "custom": 1,
        "naming_rule": "Random",
        "autoname": "hash",
        "track_changes": 1,
        "fields": [
            {"fieldname": "pos_opening_entry", "label": "POS Opening Entry", "fieldtype": "Link",
             "options": "POS Opening Entry", "in_list_view": 1},
            {"fieldname": "direction", "label": "Direction", "fieldtype": "Select",
             "options": "in\nout", "reqd": 1, "in_list_view": 1},
            {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1,
             "in_list_view": 1},
            {"fieldname": "reason", "label": "Reason", "fieldtype": "Small Text"},
            {"fieldname": "cashier", "label": "Cashier", "fieldtype": "Link", "options": "User",
             "in_list_view": 1},
            {"fieldname": "movement_time", "label": "Time", "fieldtype": "Datetime",
             "in_list_view": 1},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Cashier", "read": 1, "write": 1, "create": 1},
        ],
    })
    dt.flags.ignore_permissions = True
    dt.insert()
    frappe.clear_cache()
    return {"created": True}
@frappe.whitelist()
def setup_cash_movement():
    """Schema setup over HTTP. Admin only; the work is in _install_cash_movement."""
    frappe.only_for("System Manager")
    return _install_cash_movement()
@frappe.whitelist(methods=["POST"])
def record_cash_movement():
    """Record a drawer pay-in or pay-out against the current open shift."""
    roles.require_pos()
    data = frappe.local.form_dict
    direction = (data.get("direction") or "").strip().lower()
    if direction not in ("in", "out"):
        frappe.throw(_("Direction must be 'in' or 'out'"))
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero"))
    reason = (data.get("reason") or "").strip()

    opening = _open_shift_name()
    if not opening:
        frappe.throw(_("No open shift — open a shift first"))

    if not frappe.db.exists("DocType", "POS Cash Movement"):
        _install_cash_movement()

    doc = frappe.get_doc({
        "doctype": "POS Cash Movement",
        "pos_opening_entry": opening,
        "direction": direction,
        "amount": amount,
        "reason": reason,
        "cashier": frappe.session.user,
        "movement_time": now_datetime(),
    })
    doc.flags.ignore_permissions = True
    doc.insert()

    # Cash out of the drawer with no bill behind it. There is no other record of
    # it than this row and the movement itself, and the reason is free text.
    activity.log_activity(
        "cash.move",
        label="Drawer %s Rs %.0f%s" % ("in" if direction == "in" else "out", amount,
                                       (" — " + reason) if reason else ""),
        ref_doctype="POS Cash Movement", ref_name=doc.name,
        amount=amount if direction == "in" else -amount,
        detail={"direction": direction, "reason": reason, "opening_entry": opening})

    return {"ok": True, "name": doc.name, "direction": direction, "amount": amount}
@frappe.whitelist()
def get_cash_movements():
    """Cash-drawer movements for the current open shift: totals + recent list (newest first)."""
    roles.require_pos()
    opening = _open_shift_name()
    if not opening or not frappe.db.exists("DocType", "POS Cash Movement"):
        return {"in_total": 0, "out_total": 0, "moves": []}

    rows = frappe.get_list("POS Cash Movement",
        filters={"pos_opening_entry": opening},
        fields=["name", "direction", "amount", "reason", "cashier", "movement_time"],
        order_by="movement_time desc")
    in_total = sum(float(r.amount or 0) for r in rows if r.direction == "in")
    out_total = sum(float(r.amount or 0) for r in rows if r.direction == "out")
    moves = [{
        "direction": r.direction,
        "amount": float(r.amount or 0),
        "reason": r.reason or "",
        "cashier": frappe.get_value("User", r.cashier, "full_name") or r.cashier,
        "time": frappe.utils.pretty_date(r.movement_time),
    } for r in rows]
    return {"in_total": in_total, "out_total": out_total, "moves": moves}
