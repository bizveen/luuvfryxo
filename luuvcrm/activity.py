# -*- coding: utf-8 -*-
"""Staff activity trail — who did what, on which bill, in what order.

Why this exists: when a customer says "the bill was Rs 2,000 more than it should
be", nothing in the system could answer it. POS Order Versions record that the
items changed, not that the cashier's finger hit "+" twice in three seconds, and
they say nothing at all about payments, drawer movements, price edits or
approvals. `Client Error Log` (api.py) catches what *breaks*; this catches what
*people do*.

Three layers write into one table, so a bill's story reads top to bottom:

  1. Generic  — the `after_request` hook (`log_request_activity`) records every
                POST to a luuvcrm API method. Zero-maintenance coverage: a new
                endpoint is logged the day it ships, without anyone remembering.
  2. Semantic — explicit `log_activity()` calls inside the money endpoints in
                api.py, carrying amounts, before/after totals and flags. Those
                methods are listed in SELF_LOGGED so layer 1 stays out of the way.
  3. Client   — www/lfact.js posts the cart taps (add / +/- / remove / discount /
                fire / print / pay) with the bill total before and after each tap.
                Only the browser knows a tap happened twice; the server just sees
                one autosave with qty 2.

Everything here is fire-and-forget: a logging bug must never break the POS.
"""

import json

import frappe
from luuvcrm import roles
from frappe.utils import now_datetime, cint, flt

DOCTYPE = "LF Activity Log"

# Apps we accept in the `app` column. Anything else is stored as "other".
# "pos2"/"pos3"/"app2" are gone with the pages; rows already written keep their
# old value, since this set only normalises what is being written now.
KNOWN_APPS = {"app", "pos", "waiter", "manager", "kitchen", "purchases", "admin",
              "cus", "kiosk", "guide", "qrpromo", "errors", "activity", "server"}

# ─── Flag rules (the "human error" detector) ──────────────────────────────
FLAG_DOUBLE_TAP = "double-tap"        # same item added twice within DOUBLE_TAP_SECONDS
FLAG_AFTER_KOT = "after-kot"          # item pulled/reduced after the kitchen fired it
FLAG_BIG_DISCOUNT = "big-discount"    # discount / redeem / free item over DISCOUNT_FLAG_PCT
FLAG_CASH_VARIANCE = "cash-variance"  # shift close counted vs expected
FLAG_LATE_EDIT = "late-edit"          # edit / move / reprint after the bill was paid
FLAG_DUP_APPROVAL = "duplicate-approval"

DOUBLE_TAP_SECONDS = 3
DISCOUNT_FLAG_PCT = 10.0
CASH_VARIANCE_TOLERANCE = 100.0       # LKR, either direction
DUP_APPROVAL_WINDOW_MIN = 10

FLAG_LABELS = {
    FLAG_DOUBLE_TAP: "Double tap",
    FLAG_AFTER_KOT: "Changed after KOT",
    FLAG_BIG_DISCOUNT: "Big discount",
    FLAG_CASH_VARIANCE: "Cash variance",
    FLAG_LATE_EDIT: "Edited after payment",
    FLAG_DUP_APPROVAL: "Duplicate approval",
}

# Endpoints that call log_activity() themselves with real amounts — the generic
# after_request layer skips these so a payment isn't logged twice.
SELF_LOGGED = {
    "place_order", "update_order", "send_to_kitchen", "process_payment", "pay_split",
    "cancel_order", "delete_draft_order", "move_order_table", "record_cash_movement",
    "pos_open_shift", "pos_close_shift", "waiter_append_items", "mark_order_served",
    "update_menu_item", "add_menu_item", "create_approval", "resolve_approval",
    "create_wastage", "record_guide_payout", "log_print", "sync_offline_orders",
}

# Never logged: our own logging endpoints (which would otherwise report themselves
# forever) and the high-volume pollers.
NEVER_LOGGED = {
    "log_client_error", "log_ui_activity", "get_activity", "get_bill_trail",
    "get_activity_flags", "purge_activity_log", "setup_activity_log",
    "review_activity_flag", "get_activity_summary",
}

# Request args that must never reach the log.
SECRET_KEYS = {"pwd", "password", "new_password", "pin", "old_pin", "new_pin", "otp",
               "token", "guide_token", "csrf_token", "cmd", "key", "api_secret", "secret"}

MAX_DETAIL = 4000
# Circuit breaker: client rows accepted per hour, across all devices.
UI_HOURLY_CAP = 4000


# ─── DocType ──────────────────────────────────────────────────────────────
@frappe.whitelist()
def setup_activity_log():
    """Create the `LF Activity Log` Custom DocType (idempotent, no migrate needed).
    Runs lazily on first write, or:
      bench --site <site> execute luuvcrm.activity.setup_activity_log
    Named `LF Activity Log` because Frappe core already owns `Activity Log`.
    """
    if frappe.db.exists("DocType", DOCTYPE):
        return {"created": False, "exists": True}
    frappe.get_doc({
        "doctype": "DocType", "name": DOCTYPE, "module": "Zeloura",
        "custom": 1, "naming_rule": "Random", "autoname": "hash",
        "fields": [
            {"fieldname": "app", "label": "App", "fieldtype": "Data", "search_index": 1,
             "in_list_view": 1},
            {"fieldname": "action", "label": "Action", "fieldtype": "Data", "search_index": 1,
             "in_list_view": 1},
            {"fieldname": "label", "label": "Label", "fieldtype": "Data", "in_list_view": 1},
            {"fieldname": "source", "label": "Source", "fieldtype": "Select",
             "options": "server\nclient\nrequest", "default": "server"},
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
            {"fieldname": "amount", "label": "Amount", "fieldtype": "Float", "precision": "2",
             "in_list_view": 1},
            {"fieldname": "total_before", "label": "Total Before", "fieldtype": "Float",
             "precision": "2"},
            {"fieldname": "total_after", "label": "Total After", "fieldtype": "Float",
             "precision": "2"},
            # The point of the whole thing
            {"fieldname": "flag", "label": "Flagged", "fieldtype": "Check", "default": "0",
             "search_index": 1, "in_list_view": 1},
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
            {"fieldname": "session_id", "label": "Session", "fieldtype": "Data",
             "search_index": 1},
            {"fieldname": "seq", "label": "Seq", "fieldtype": "Int"},
            {"fieldname": "client_ts", "label": "Client Time", "fieldtype": "Datetime"},
            {"fieldname": "ok", "label": "OK", "fieldtype": "Check", "default": "1"},
            {"fieldname": "err", "label": "Error", "fieldtype": "Small Text"},
            {"fieldname": "ms", "label": "Duration ms", "fieldtype": "Int"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        ],
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache()
    return {"created": True}


def _ensure_doctype():
    if not frappe.db.exists("DocType", DOCTYPE):
        setup_activity_log()


# ─── Writer ───────────────────────────────────────────────────────────────
def _clip(v, n=300):
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    return s[:n]


def _sanitize(d):
    """Drop secrets and oversized blobs before anything reaches the log."""
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        lk = str(k).lower()
        if lk in SECRET_KEYS or "password" in lk or lk.endswith("_pin"):
            continue
        if isinstance(v, (dict, list)):
            try:
                v = json.dumps(v, default=str)
            except Exception:
                v = str(v)
        out[str(k)[:40]] = _clip(v, 600)
    return out


def log_activity(action, app=None, label=None, source="server", user=None,
                 ref_doctype=None, ref_name=None, bill_no=None, table=None,
                 item=None, item_name=None, qty=None, amount=None,
                 total_before=None, total_after=None,
                 flag_reason=None, flag_note=None, detail=None,
                 page=None, device=None, session_id=None, seq=None,
                 client_ts=None, ok=1, err=None, ms=None, commit=True):
    """Record one staff action. Never raises — the caller is a live POS."""
    try:
        _ensure_doctype()
        if user is None:
            user = frappe.session.user or "Guest"
        if app is None:
            app = _app_from_request()
        if app not in KNOWN_APPS:
            app = "other"
        if detail is not None and not isinstance(detail, str):
            try:
                detail = json.dumps(_sanitize(detail) if isinstance(detail, dict) else detail,
                                    default=str)
            except Exception:
                detail = str(detail)

        doc = frappe.get_doc({
            "doctype": DOCTYPE,
            "app": app,
            "action": _clip(action, 60),
            "label": _clip(label or action, 140),
            "source": source if source in ("server", "client", "request") else "server",
            "user": _clip(user, 140),
            "user_full": _clip(_full_name(user), 140),
            "ref_doctype": _clip(ref_doctype, 60),
            "ref_name": _clip(ref_name, 140),
            "bill_no": _clip(bill_no, 60),
            "table": _clip(table, 60),
            "item": _clip(item, 140),
            "item_name": _clip(item_name, 140),
            "qty": flt(qty) if qty is not None else None,
            "amount": flt(amount) if amount is not None else None,
            "total_before": flt(total_before) if total_before is not None else None,
            "total_after": flt(total_after) if total_after is not None else None,
            "flag": 1 if flag_reason else 0,
            "flag_reason": _clip(flag_reason, 60),
            "flag_note": _clip(flag_note, 300),
            "detail": _clip(detail, MAX_DETAIL),
            "page": _clip(page or _request_path(), 300),
            "ip": _clip(_client_ip(), 60),
            "device": _clip(device or _user_agent(), 300),
            "session_id": _clip(session_id, 60),
            "seq": cint(seq) if seq is not None else None,
            "client_ts": client_ts or None,
            "ok": 1 if ok else 0,
            "err": _clip(err, 300),
            "ms": cint(ms) if ms is not None else None,
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        if commit:
            frappe.db.commit()
        return doc.name
    except Exception:
        try:
            frappe.log_error(frappe.get_traceback(), "log_activity failed")
        except Exception:
            pass
        return None


_name_cache = {}


def _full_name(user):
    if not user or user in ("Guest", "Administrator"):
        return user or ""
    if user not in _name_cache:
        try:
            _name_cache[user] = frappe.db.get_value("User", user, "full_name") or user.split("@")[0]
        except Exception:
            _name_cache[user] = user
    return _name_cache[user]


def _request_path():
    try:
        return frappe.request.path if frappe.request else ""
    except Exception:
        return ""


def _client_ip():
    try:
        return frappe.local.request_ip or ""
    except Exception:
        return ""


def _user_agent():
    try:
        return frappe.get_request_header("User-Agent") or ""
    except Exception:
        return ""


def _app_from_request():
    """Best guess at which PWA made the call, from the Referer it was fired from."""
    try:
        ref = frappe.get_request_header("Referer") or ""
        if ref:
            tail = ref.split("://", 1)[-1]
            path = tail[tail.find("/"):] if "/" in tail else ""
            first = path.strip("/").split("?")[0].split("/")[0]
            if first in KNOWN_APPS:
                return first
    except Exception:
        pass
    return "server"


# ─── Layer 1: generic coverage via the after_request hook ─────────────────
def log_request_activity(response=None, request=None, **kwargs):
    """Log every POST to a luuvcrm API method.

    Wired from hooks.py `after_request`. Frappe runs this in the request's
    `finally` block, AFTER sync_database has already committed (or rolled back on
    error), so this writes and commits on its own. Frappe swallows exceptions
    raised here, but we catch anyway — this must never colour a response.
    """
    try:
        if request is None or (request.method or "").upper() != "POST":
            return
        path = request.path or ""
        if "/api/method/luuvcrm." not in path:
            return
        method = path.rsplit(".", 1)[-1].strip("/")
        if not method or method in NEVER_LOGGED or method in SELF_LOGGED:
            return
        if method.startswith("setup_") or method.startswith("get_") or method.startswith("debug_"):
            return

        status = getattr(response, "status_code", 200) or 200
        args = {}
        try:
            args = _sanitize(dict(request.form or {}))
        except Exception:
            args = {}

        log_activity(
            action=method,
            label=method.replace("_", " ").strip().capitalize(),
            source="request",
            ref_name=_clip(args.get("order_name") or args.get("name") or "", 140),
            detail=args,
            page=path,
            ok=1 if status < 400 else 0,
            err="" if status < 400 else "HTTP %s" % status,
        )
    except Exception:
        pass


# ─── Layer 3: client tap trail (www/lfact.js posts here) ──────────────────
@frappe.whitelist(methods=["POST"])
def log_ui_activity():
    """Accept a batch of UI taps from a staff PWA.

    Login-only (unlike log_client_error) — every app that writes here has a
    signed-in staffer, and an unauthenticated writer could flood the table.
    Always returns ok; a logger that throws would break the app it watches.
    """
    try:
        _ensure_doctype()
        raw = frappe.local.form_dict.get("events")
        events = frappe.parse_json(raw) if isinstance(raw, str) else (raw or [])
        if not isinstance(events, list) or not events:
            return {"ok": False}
        events = events[:60]

        recent = frappe.db.count(DOCTYPE, {
            "source": "client",
            "creation": (">", frappe.utils.add_to_date(now_datetime(), hours=-1)),
        })
        if recent >= UI_HOURLY_CAP:
            return {"ok": False, "throttled": True}

        app = (frappe.local.form_dict.get("app") or _app_from_request())[:40]
        session_id = (frappe.local.form_dict.get("session_id") or "")[:60]
        device = (frappe.local.form_dict.get("ua") or _user_agent())[:300]
        written = 0

        for ev in events:
            if not isinstance(ev, dict):
                continue
            action = (ev.get("action") or "").strip()[:60]
            if not action:
                continue
            flag_reason = ev.get("flag") or None
            if flag_reason not in FLAG_LABELS:
                flag_reason = None
            log_activity(
                action=action,
                app=app,
                label=(ev.get("label") or action)[:140],
                source="client",
                ref_doctype="POS Order" if ev.get("order") else None,
                ref_name=ev.get("order"),
                bill_no=ev.get("bill"),
                table=ev.get("table"),
                item=ev.get("item"),
                item_name=ev.get("item_name"),
                qty=ev.get("qty"),
                amount=ev.get("amount"),
                total_before=ev.get("before"),
                total_after=ev.get("after"),
                flag_reason=flag_reason,
                flag_note=ev.get("flag_note"),
                detail=ev.get("detail"),
                page=ev.get("page"),
                device=device,
                session_id=session_id,
                seq=ev.get("seq"),
                client_ts=_parse_client_ts(ev.get("ts")),
                commit=False,
            )
            written += 1

        frappe.db.commit()
        return {"ok": True, "written": written}
    except Exception:
        try:
            frappe.log_error(frappe.get_traceback(), "log_ui_activity failed")
        except Exception:
            pass
        return {"ok": False}


def _parse_client_ts(ts):
    """Client sends epoch milliseconds; an offline queue can replay hours later,
    so the device's own clock is kept alongside our `creation`."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(ts) / 1000.0)
    except Exception:
        return None


# ─── Reading it back ──────────────────────────────────────────────────────
# luuvcrm/roles.py imports neither this module nor api.py, which is what lets
# both share it — api.py imports activity.py, so the copy that used to live here
# could not have come from there.


ACTIVITY_FIELDS = [
    "name", "creation", "app", "action", "label", "source", "user", "user_full",
    "ref_doctype", "ref_name", "bill_no", "table", "item", "item_name", "qty",
    "amount", "total_before", "total_after", "flag", "flag_reason", "flag_note",
    "reviewed", "reviewed_by", "detail", "page", "device", "session_id", "seq",
    "client_ts", "ok", "err",
]


def _shape(rows):
    for r in rows:
        r["creation"] = str(r.get("creation") or "")
        r["client_ts"] = str(r.get("client_ts") or "")
        r["flag_label"] = FLAG_LABELS.get(r.get("flag_reason") or "", r.get("flag_reason") or "")
        r["who"] = r.get("user_full") or r.get("user") or ""
    return rows


@frappe.whitelist()
def get_activity(app=None, user=None, action=None, flagged=None, days=1,
                 bill=None, search=None, limit=200):
    """Activity feed for /activity. Newest first."""
    roles.require_manager()
    if not frappe.db.exists("DocType", DOCTYPE):
        return {"rows": [], "apps": [], "users": [], "flag_count": 0}

    try:
        limit = max(1, min(int(limit or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        days = max(0, min(int(days or 1), 90))
    except (TypeError, ValueError):
        days = 1

    filters = {}
    if days:
        filters["creation"] = (">=", frappe.utils.add_to_date(now_datetime(), days=-days))
    if app and app != "all":
        filters["app"] = app
    if user and user != "all":
        filters["user"] = user
    if action:
        filters["action"] = action
    if bill:
        filters["bill_no"] = bill
    if cint(flagged):
        filters["flag"] = 1
        filters["reviewed"] = 0

    or_filters = None
    if search:
        s = "%%%s%%" % search.strip()[:60]
        or_filters = {"label": ("like", s), "item_name": ("like", s),
                      "bill_no": ("like", s), "ref_name": ("like", s),
                      "user_full": ("like", s)}

    rows = frappe.get_all(DOCTYPE, filters=filters, or_filters=or_filters,
                          fields=ACTIVITY_FIELDS,
                          order_by="creation desc, seq desc", limit_page_length=limit)

    # Filter pills — built from the same window the list is showing.
    window = {"creation": filters["creation"]} if days else {}
    apps = [r.app for r in frappe.get_all(DOCTYPE, filters=window, fields=["distinct app as app"])
            if r.app]
    users = [{"user": r.user, "name": r.user_full or r.user}
             for r in frappe.get_all(DOCTYPE, filters=window,
                                     fields=["distinct user as user", "user_full"])
             if r.user]
    flag_count = frappe.db.count(DOCTYPE, dict(window, flag=1, reviewed=0))

    return {"rows": _shape(rows), "apps": sorted(apps),
            "users": sorted(users, key=lambda u: u["name"]),
            "flag_count": flag_count,
            "flag_labels": FLAG_LABELS}


@frappe.whitelist()
def get_bill_trail(order=None, bill=None):
    """Every tap and every server action for ONE bill, oldest first.

    This is the answer to "the bill was Rs 2,000 more than it should be": the
    cashier taps and the server writes interleaved on a single timeline.
    Accepts either the POS Order name or the printed bill number.
    """
    roles.require_manager()
    if not frappe.db.exists("DocType", DOCTYPE):
        return {"rows": [], "order": order or "", "bill": bill or ""}

    order = (order or "").strip()
    bill = (bill or "").strip()
    if not order and not bill:
        return {"rows": [], "order": "", "bill": ""}

    or_filters = {}
    if order:
        or_filters["ref_name"] = order
    if bill:
        or_filters["bill_no"] = bill
    rows = frappe.get_all(DOCTYPE, or_filters=or_filters, fields=ACTIVITY_FIELDS,
                          order_by="creation asc, seq asc", limit_page_length=500)

    head = {}
    if order and frappe.db.exists("POS Order", order):
        o = frappe.db.get_value("POS Order", order,
                                ["name", "restaurant_table", "grand_total", "customer_name",
                                 "pos_invoice", "owner", "creation", "docstatus"], as_dict=True)
        if o:
            head = {"order": o.name, "table": o.restaurant_table or "",
                    "grand_total": flt(o.grand_total), "customer": o.customer_name or "",
                    "invoice": o.pos_invoice or "", "opened_by": _full_name(o.owner),
                    "opened_at": str(o.creation), "docstatus": o.docstatus}

    return {"rows": _shape(rows), "order": order, "bill": bill, "head": head,
            "flag_labels": FLAG_LABELS}


@frappe.whitelist()
def get_activity_flags(days=1):
    """Unreviewed flag count + a per-reason breakdown — feeds the /manager badge."""
    roles.require_manager()
    if not frappe.db.exists("DocType", DOCTYPE):
        return {"count": 0, "by_reason": []}
    try:
        days = max(1, min(int(days or 1), 90))
    except (TypeError, ValueError):
        days = 1
    since = frappe.utils.add_to_date(now_datetime(), days=-days)
    rows = frappe.db.sql("""
        select flag_reason, count(*) as n, sum(ifnull(amount, 0)) as amount
        from `tabLF Activity Log`
        where flag = 1 and reviewed = 0 and creation >= %s
        group by flag_reason order by n desc
    """, (since,), as_dict=True)
    return {"count": sum(cint(r.n) for r in rows),
            "by_reason": [{"reason": r.flag_reason,
                           "label": FLAG_LABELS.get(r.flag_reason, r.flag_reason),
                           "count": cint(r.n), "amount": flt(r.amount)} for r in rows]}


@frappe.whitelist(methods=["POST"])
def review_activity_flag(name=None, note="", reviewed=1):
    """Manager ticks off a flag once they have checked it (or un-ticks it)."""
    roles.require_manager()
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw("Activity row not found")
    done = cint(reviewed)
    payload = {
        "reviewed": 1 if done else 0,
        "reviewed_by": frappe.session.user if done else "",
        "reviewed_at": now_datetime() if done else None,
    }
    if (note or "").strip():
        payload["flag_note"] = note.strip()[:300]
    frappe.db.set_value(DOCTYPE, name, payload, update_modified=False)
    frappe.db.commit()
    return {"ok": True}


# ─── Retention ────────────────────────────────────────────────────────────
KEEP_DAYS_PLAIN = 90
KEEP_DAYS_FLAGGED = 365


def purge_activity_log():
    """Daily scheduler job: plain rows older than 90 days, flagged rows older than
    a year. The POS writes a lot and this runs on a small VPS."""
    try:
        if not frappe.db.exists("DocType", DOCTYPE):
            return
        now = now_datetime()
        frappe.db.sql("""delete from `tabLF Activity Log`
                         where flag = 0 and creation < %s""",
                      (frappe.utils.add_to_date(now, days=-KEEP_DAYS_PLAIN),))
        frappe.db.sql("""delete from `tabLF Activity Log`
                         where flag = 1 and creation < %s""",
                      (frappe.utils.add_to_date(now, days=-KEEP_DAYS_FLAGGED),))
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "purge_activity_log failed")


@frappe.whitelist(methods=["POST"])
def clear_activity(scope="reviewed"):
    """Manual clear from /activity — the scheduler is not always running on this
    bench (same reason shift close once said "Scheduler is inactive"), so the
    manager gets a button too."""
    roles.require_manager()
    if not frappe.db.exists("DocType", DOCTYPE):
        return {"ok": True, "deleted": 0}
    if scope == "all":
        n = frappe.db.count(DOCTYPE)
        frappe.db.sql("delete from `tabLF Activity Log`")
    elif scope == "unflagged":
        n = frappe.db.count(DOCTYPE, {"flag": 0})
        frappe.db.sql("delete from `tabLF Activity Log` where flag = 0")
    else:
        n = frappe.db.count(DOCTYPE, {"reviewed": 1})
        frappe.db.sql("delete from `tabLF Activity Log` where reviewed = 1")
    frappe.db.commit()
    return {"ok": True, "deleted": n}


# ─── Flag helpers used by api.py ──────────────────────────────────────────
def discount_flag(discount_amount, order_total):
    """Flag a discount/redeem worth more than DISCOUNT_FLAG_PCT of the bill."""
    try:
        if flt(order_total) <= 0 or flt(discount_amount) <= 0:
            return None, ""
        pct = flt(discount_amount) / flt(order_total) * 100.0
        if pct >= DISCOUNT_FLAG_PCT:
            return FLAG_BIG_DISCOUNT, "%.0f%% of the bill (Rs %.0f)" % (pct, flt(discount_amount))
    except Exception:
        pass
    return None, ""


def duplicate_approval_flag(order_name, approval_type):
    """A second approval request of the same type for the same order inside
    DUP_APPROVAL_WINDOW_MIN — the cashier tapped twice, or asked two managers."""
    try:
        if not order_name:
            return None, ""
        since = frappe.utils.add_to_date(now_datetime(), minutes=-DUP_APPROVAL_WINDOW_MIN)
        n = frappe.db.count(DOCTYPE, {
            "action": "approval.create",
            "ref_name": order_name,
            "item": approval_type or "",
            "creation": (">=", since),
        })
        if n:
            return FLAG_DUP_APPROVAL, "%d earlier %s request(s) in %d min" % (
                n, approval_type or "approval", DUP_APPROVAL_WINDOW_MIN)
    except Exception:
        pass
    return None, ""


def kot_pull_flag(removed_fired_qty, item_name):
    """Item that the kitchen had already fired was removed or reduced."""
    try:
        n = flt(removed_fired_qty)
        if n > 0:
            return FLAG_AFTER_KOT, "%g x %s pulled after the KOT" % (n, item_name or "item")
    except Exception:
        pass
    return None, ""


def late_edit_flag(was_paid, what="edited"):
    """Anything done to a bill after it was already paid/invoiced."""
    if was_paid:
        return FLAG_LATE_EDIT, "bill %s after payment" % what
    return None, ""


def variance_flag(difference, mode):
    """Shift-close counted vs expected, beyond CASH_VARIANCE_TOLERANCE."""
    try:
        d = flt(difference)
        if abs(d) > CASH_VARIANCE_TOLERANCE:
            return FLAG_CASH_VARIANCE, "%s %s Rs %.0f" % (
                mode or "Cash", "short" if d < 0 else "over", abs(d))
    except Exception:
        pass
    return None, ""


def reprint_flag(order_name, print_type):
    """Second (or third) print of the same document for the same bill. A reprint is
    normal once; a habit of reprinting the guest check is how a bill gets swapped."""
    try:
        if not order_name:
            return None, ""
        n = frappe.db.count(DOCTYPE, {"action": "print", "ref_name": order_name,
                                      "item": print_type or ""})
        if n:
            return FLAG_LATE_EDIT, "reprint #%d of the %s" % (n + 1, print_type or "receipt")
    except Exception:
        pass
    return None, ""
