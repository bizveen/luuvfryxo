"""The front-end error log: what breaks on a staff phone, and who gets told.

`www/lferr.js` is the first script on every staff PWA. It catches uncaught
errors, unhandled rejections, resources that failed to load, anything passed to
console.error, and any fetch that returned 5xx or died at the network layer —
and posts each one here. `/errors` reads them back and `/admin` shows the open
count as a badge.

None of that worked. The endpoints were never written: `lferr.js` shipped
pointing at `luuvcrm.api.log_client_error`, which did not exist, and because the
reporter is deliberately fire-and-forget inside a try/catch, every report 404'd
in silence. The pages that read the log answered nothing. So the one thing built
to tell somebody the app is broken was itself broken, quietly, for its whole
life.

Two things this has to get right:

**Deduplicate.** A render loop throwing on every frame, on eight devices, is one
bug. `lferr.js` already suppresses a repeat within a page load, but not across
loads or devices. Rows are keyed by a fingerprint of (app, kind, message,
source, line): the same bug bumps `hits` and `last_seen` instead of inserting.
Without that the table grows faster than anyone can read it and the page becomes
useless exactly when it matters.

**Never raise.** The caller is a page that is already broken. A logger that
throws turns a bad render into a white screen.
"""

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

from luuvcrm import roles

CLIENT_ERROR_DOCTYPE = "LF Client Error"

CLIENT_ERROR_KEEP_RESOLVED_DAYS = 30
CLIENT_ERROR_KEEP_OPEN_DAYS = 180

CLIENT_ERROR_KINDS = ("js", "promise", "resource", "console", "api", "manual")

# What a single report may carry. Anything else in the request is dropped rather
# than stored: the payload is shaped by a script on a device we do not control.
_CLIENT_ERROR_LIMITS = {
    "message": 1000, "stack": 4000, "source": 300, "page": 300,
    "ua": 300, "screen": 40, "kind": 20, "app": 40,
}


# ─── The table ───────────────────────────────────────────────────────────────

def _install_client_errors():
    """Create `LF Client Error` (idempotent, no migrate needed).

    Same runtime-install pattern as `LF Activity Log`; Phase 3 moves all of them
    into the repo together. Named LF-prefixed because Frappe core owns `Error
    Log` and this is not it — that one holds server tracebacks.
    """
    if frappe.db.exists("DocType", CLIENT_ERROR_DOCTYPE):
        return {"created": False, "exists": True}
    frappe.get_doc({
        "doctype": "DocType", "name": CLIENT_ERROR_DOCTYPE, "module": "Zeloura",
        "custom": 1, "naming_rule": "Random", "autoname": "hash",
        "fields": [
            # the identity of a bug, not of a report
            {"fieldname": "fingerprint", "label": "Fingerprint", "fieldtype": "Data",
             "unique": 1, "search_index": 1},
            {"fieldname": "kind", "label": "Kind", "fieldtype": "Data",
             "search_index": 1, "in_list_view": 1},
            {"fieldname": "app", "label": "App", "fieldtype": "Data",
             "search_index": 1, "in_list_view": 1},
            {"fieldname": "message", "label": "Message", "fieldtype": "Text",
             "in_list_view": 1},
            {"fieldname": "source", "label": "Source", "fieldtype": "Data"},
            {"fieldname": "line", "label": "Line", "fieldtype": "Int"},
            {"fieldname": "col", "label": "Column", "fieldtype": "Int"},
            {"fieldname": "stack", "label": "Stack", "fieldtype": "Long Text"},
            # where and on what
            {"fieldname": "page", "label": "Page", "fieldtype": "Data"},
            {"fieldname": "ua", "label": "Device", "fieldtype": "Small Text"},
            {"fieldname": "screen", "label": "Screen", "fieldtype": "Data"},
            {"fieldname": "user", "label": "User", "fieldtype": "Data", "search_index": 1},
            # how often, and whether anybody has dealt with it
            {"fieldname": "hits", "label": "Hits", "fieldtype": "Int", "default": "1",
             "in_list_view": 1},
            {"fieldname": "first_seen", "label": "First Seen", "fieldtype": "Datetime"},
            {"fieldname": "last_seen", "label": "Last Seen", "fieldtype": "Datetime",
             "search_index": 1},
            {"fieldname": "status", "label": "Status", "fieldtype": "Select",
             "options": "open\nresolved", "default": "open", "search_index": 1,
             "in_list_view": 1},
            {"fieldname": "resolved_by", "label": "Resolved By", "fieldtype": "Data"},
            {"fieldname": "resolved_at", "label": "Resolved At", "fieldtype": "Datetime"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
        ],
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache()
    return {"created": True}


@frappe.whitelist()
def setup_client_errors():
    """Schema setup over HTTP. Admin only; the work is in _install_client_errors."""
    roles.require_admin()
    return _install_client_errors()


def _ensure_client_error_doctype():
    if not frappe.db.exists("DocType", CLIENT_ERROR_DOCTYPE):
        _install_client_errors()


# ─── Writing ─────────────────────────────────────────────────────────────────

def _clip_text(value, limit):
    text = "" if value is None else str(value)
    return text[:limit]


def _client_error_fingerprint(app, kind, message, source, line):
    """What makes two reports the same bug.

    Deliberately not the stack: the same fault from two code paths is worth
    seeing twice, but the same fault on 8 tills is one row. Not the page either
    — a bug in a shared script would otherwise split across every route it is
    loaded on and none of the rows would look urgent.
    """
    raw = "|".join((app or "", kind or "", message or "", source or "", str(line or 0)))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


@frappe.whitelist(methods=["POST"])
def log_client_error():
    """Record one front-end error. Never raises, never throws to the caller.

    Login-only, not guest: every page that loads lferr.js is behind a staff
    login, and an open endpoint that writes a row per request is a way to fill
    somebody's disk.
    """
    try:
        _ensure_client_error_doctype()
        data = frappe.local.form_dict
        message = _clip_text(data.get("message"), _CLIENT_ERROR_LIMITS["message"]).strip()
        if not message:
            return {"ok": False}

        kind = _clip_text(data.get("kind"), _CLIENT_ERROR_LIMITS["kind"]) or "js"
        if kind not in CLIENT_ERROR_KINDS:
            kind = "js"
        app = _clip_text(data.get("app"), _CLIENT_ERROR_LIMITS["app"]) or "unknown"
        source = _clip_text(data.get("source"), _CLIENT_ERROR_LIMITS["source"])
        line = frappe.utils.cint(data.get("line"))
        fp = _client_error_fingerprint(app, kind, message, source, line)
        now = now_datetime()

        existing = frappe.db.get_value(CLIENT_ERROR_DOCTYPE, {"fingerprint": fp}, ["name", "hits"],
                                       as_dict=True)
        if existing:
            # The same bug again. Bump it rather than inserting — and leave
            # `status` alone: a manager who marked it fixed wants to see it
            # reopen only if they choose to, not on the next stray hit.
            frappe.db.set_value(CLIENT_ERROR_DOCTYPE, existing.name, {
                "hits": (existing.hits or 1) + 1,
                "last_seen": now,
            }, update_modified=False)
            frappe.db.commit()
            return {"ok": True, "name": existing.name, "repeat": True}

        doc = frappe.get_doc({
            "doctype": CLIENT_ERROR_DOCTYPE,
            "fingerprint": fp,
            "kind": kind,
            "app": app,
            "message": message,
            "source": source,
            "line": line,
            "col": frappe.utils.cint(data.get("col")),
            "stack": _clip_text(data.get("stack"), _CLIENT_ERROR_LIMITS["stack"]),
            "page": _clip_text(data.get("page"), _CLIENT_ERROR_LIMITS["page"]),
            "ua": _clip_text(data.get("ua"), _CLIENT_ERROR_LIMITS["ua"]),
            "screen": _clip_text(data.get("screen"), _CLIENT_ERROR_LIMITS["screen"]),
            "user": frappe.session.user,
            "hits": 1,
            "first_seen": now,
            "last_seen": now,
            "status": "open",
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"ok": True, "name": doc.name, "repeat": False}
    except Exception:
        # The caller is a page that has already broken. Swallow, and leave a
        # trace somewhere a developer will find it.
        frappe.db.rollback()
        try:
            frappe.log_error(frappe.get_traceback(), "log_client_error failed")
        except Exception:
            pass
        return {"ok": False}


# ─── Reading ─────────────────────────────────────────────────────────────────

def _client_error_row(doc):
    last = doc.get("last_seen")
    return {
        "name": doc.name,
        "kind": doc.kind or "js",
        "app": doc.app or "unknown",
        "message": doc.message or "",
        "source": doc.source or "",
        "line": doc.line or 0,
        "page": doc.page or "",
        "user": doc.user or "",
        "stack": doc.stack or "",
        "ua": doc.ua or "",
        "screen": doc.screen or "",
        "hits": doc.hits or 1,
        "status": doc.status or "open",
        "first_seen": str(doc.get("first_seen") or ""),
        "last_seen": str(last or ""),
        "when": frappe.utils.pretty_date(last) if last else "",
    }


@frappe.whitelist()
def get_client_errors(status="open", app=None, limit=150):
    """The error list behind /errors, and the badge count on /admin.

    Manager-only: a row carries a stack trace, the page a staffer was on, their
    user id and their device string. That is a staff-surveillance surface as
    much as a debugging one, so it sits behind the same gate as the reports.
    """
    roles.require_manager()
    if not frappe.db.exists("DocType", CLIENT_ERROR_DOCTYPE):
        return {"errors": [], "apps": [], "open_count": 0}

    filters = {}
    if status:
        filters["status"] = status
    if app:
        filters["app"] = app

    names = frappe.get_all(CLIENT_ERROR_DOCTYPE, filters=filters, pluck="name",
                           order_by="last_seen desc",
                           limit_page_length=frappe.utils.cint(limit) or 150)
    errors = [_client_error_row(frappe.get_doc(CLIENT_ERROR_DOCTYPE, n)) for n in names]

    # The chips on /errors list every app that has ever reported, not only the
    # ones in this filtered page — otherwise filtering to an app hides the chip
    # that would let you filter back out of it.
    apps = sorted({a for a in frappe.get_all(CLIENT_ERROR_DOCTYPE, pluck="app") if a})
    return {
        "errors": errors,
        "apps": apps,
        "open_count": frappe.db.count(CLIENT_ERROR_DOCTYPE, {"status": "open"}),
    }


@frappe.whitelist(methods=["POST"])
def resolve_client_error(name=None, status="resolved"):
    """Mark one row fixed, or reopen it."""
    roles.require_manager()
    if status not in ("open", "resolved"):
        frappe.throw(_("Unknown status"))
    if not name or not frappe.db.exists(CLIENT_ERROR_DOCTYPE, name):
        return {"ok": False, "error": "Not found"}
    frappe.db.set_value(CLIENT_ERROR_DOCTYPE, name, {
        "status": status,
        "resolved_by": frappe.session.user if status == "resolved" else None,
        "resolved_at": now_datetime() if status == "resolved" else None,
    })
    frappe.db.commit()
    return {"ok": True, "name": name, "status": status}


@frappe.whitelist(methods=["POST"])
def clear_client_errors(scope="resolved"):
    """Delete rows already marked fixed.

    `scope` is only ever "resolved". It is a parameter because /errors sends it,
    and it is validated rather than ignored so that a future "all" cannot be
    reached by guessing before anybody has decided it should exist.
    """
    roles.require_manager()
    if scope != "resolved":
        frappe.throw(_("Only resolved rows can be cleared"))
    if not frappe.db.exists("DocType", CLIENT_ERROR_DOCTYPE):
        return {"ok": True, "deleted": 0}
    names = frappe.get_all(CLIENT_ERROR_DOCTYPE, filters={"status": "resolved"}, pluck="name")
    for name in names:
        frappe.delete_doc(CLIENT_ERROR_DOCTYPE, name, force=True, ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "deleted": len(names)}


# ─── Keeping the table small ─────────────────────────────────────────────────

def purge_client_errors():
    """Daily scheduler job. Resolved rows go after a month, open ones after six.

    Open rows are kept longer on purpose: an old open row is either still broken
    or was never looked at, and both are worth seeing. Sibling of
    activity.purge_activity_log — same reasoning, same small VPS.
    """
    try:
        if not frappe.db.exists("DocType", CLIENT_ERROR_DOCTYPE):
            return
        now = now_datetime()
        frappe.db.sql("""delete from `tabLF Client Error`
                         where status = 'resolved' and last_seen < %s""",
                      (frappe.utils.add_to_date(now, days=-CLIENT_ERROR_KEEP_RESOLVED_DAYS),))
        frappe.db.sql("""delete from `tabLF Client Error`
                         where status = 'open' and last_seen < %s""",
                      (frappe.utils.add_to_date(now, days=-CLIENT_ERROR_KEEP_OPEN_DAYS),))
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "purge_client_errors failed")
