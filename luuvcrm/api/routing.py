"""/app: Frappe Desk for staff, the customer PWA for everyone else.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
import string


# ─── /app routing: Frappe Desk vs customer PWA ───────────────────────────────
# The customer PWA used to live at /app, which shadowed Frappe Desk (Desk is
# served by frappe/www/app.py, and a later-installed app's www/app.* wins).
# The PWA now lives at /cus; this before_request hook keeps every old
# /app entry point working: WhatsApp links (/app?phone=), referral links
# (/app?ref=), promo landings (/app?promo=) and installed PWAs whose
# start_url is still /app. Desk users get Desk; everyone else gets /cus.

def route_app_requests():
    """before_request hook (hooks.py). Bare /app is pure Frappe (login + Desk);
    only customer deep links — ?promo= (promo landings), ?ref= (referrals),
    ?phone= (WhatsApp) — are redirected to the customer PWA at /cus,
    preserving the query string. Desk subpaths (/app/<doctype>...) are never
    touched. (Installed customer PWAs from the /app era land on the login
    page; they reinstall from /cus — acceptable, the app just launched.)"""
    request = getattr(frappe.local, "request", None)
    if request is None or request.path.rstrip("/") != "/app":
        return
    deep_link = request.args.get("promo") or request.args.get("ref") or request.args.get("phone")
    if not deep_link:
        return  # bare /app → Frappe default (login screen / Desk)
    qs = request.query_string.decode() if request.query_string else ""
    location = "/cus" + (("?" + qs) if qs else "")
    # Redirect from a before_request hook: frappe.Redirect here isn't special-cased by
    # handle_exception (it renders a 301 "message" page with NO Location). app.py instead
    # does `except HTTPException: return e` — so raise a werkzeug redirect (an HTTPException),
    # which is returned directly with a proper Location header.
    from werkzeug.routing import RequestRedirect
    exc = RequestRedirect(location)
    exc.code = 302
    raise exc
