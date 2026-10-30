"""/pos — the POS screen. The one at the counter.

There were three of these: /pos (superseded), /pos2 (live) and /pos3 (a UI
staging copy of pos2 taken 2026-08-26, on which all the tablet, printer-bridge
and Google-Docs-template work then happened). pos3 was the complete one, so it
is what survives; the old /pos is gone, /pos2 is now four lines of redirect for
the tablets still bookmarked to it, and this file is the only POS context
builder.

It only READS — menu, tables, shift, POS profiles. Keep it that way: backend
behaviour belongs in an api.py endpoint, not in a page context builder, so the
kiosk and the waiter screen get the same answer as the till.
"""

import frappe
from frappe import _

from luuvcrm import branch, roles
from luuvcrm.api import _get_pos_price_list, _resolve_item_rate, menu_items, tables

no_cache = 1

# ─── Google-Docs print templates ────────────────────────────────────────────
# The Guest Check / KOT / Receipt layouts live in three Google Docs so the
# manager can change wording, order or spacing without a deploy. Each doc is
# shared "Anyone with the link – Viewer" and read here as plain text through
# the export URL. The syntax the docs use is documented in "LF Print - README"
# and rendered client-side by LFTemplate in pos.html.
#
# Still here rather than in api.py because it is the page's own data, not a
# business endpoint; move it if a second screen ever needs the templates.
# Table-based docs (2026-08-26 evening): each doc is one two-column table sized
# to the 80 mm roll, so the cashier lays the receipt out cell by cell. The txt
# export renders a table row as "left<TAB>right"; LFTemplate turns that into
# its "left | right" column syntax.
# v3 docs (2026-08-26 night): pure formatting — bold / centred merged cells /
# 14pt titles instead of the = > * markers. Must be shared "Anyone with the
# link – Viewer" or the export returns a 401 login page.
PRINT_TEMPLATE_DOCS = {
    "guest_check": "1mm7END8RlwTuAZ0a0ryc2byzBcz9ASlWnQFoDWQ3oe4",
    "kot":         "1KaMNISg6so4QnYvNXxBFxVPKCHJAhLwf7vwaWvhfmdI",
    "receipt":     "1fWz6M4FbEKnnHSbLoD8cv7MkpXXIQ4vg2AsfPU5Xck0",
    "zreport":     "1fBf5WnAwLbDIC5mdeYTHO_xcfqY5MtAmIZFZjpeXO9Q",   # shift-close Z report (2026-08-28)
}
# HTML, not txt: the HTML export keeps per-cell alignment, bold, font size,
# merged cells and column widths, so the doc's own formatting drives the
# print (LFTemplate.htmlRows). The txt export flattens all of that away.
_DOC_EXPORT = "https://docs.google.com/document/d/{id}/export?format=html"
_TPL_TTL = 120            # seconds a fetched doc is served from cache
_TPL_LAST_GOOD_TTL = 0    # last-good copies never expire — a dead Google must not stop bills


@frappe.whitelist()
def get_print_templates():
    """All three templates as {kind: text}. Cached 2 min; falls back to the
    last good copy when Google is unreachable, and to "" (client uses its
    built-in layout) when there has never been one."""
    import requests
    out, source = {}, {}
    for kind, doc_id in PRINT_TEMPLATE_DOCS.items():
        ck = f"lf_print_tpl:{kind}"
        cached = frappe.cache().get_value(ck)
        if cached:
            out[kind], source[kind] = cached, "cache"
            continue
        try:
            r = requests.get(_DOC_EXPORT.format(id=doc_id), timeout=6, allow_redirects=True)
            ctype = (r.headers.get("content-type") or "")
            text = r.content.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
            # a login page is also text/html — the doc itself always carries a table
            ok = r.ok and "text/html" in ctype and "<table" in text
            if ok:
                frappe.cache().set_value(ck, text, expires_in_sec=_TPL_TTL)
                frappe.cache().set_value(ck + ":last_good", text)
                out[kind], source[kind] = text, "google"
                continue
            frappe.logger("pos").warning(f"print template {kind}: HTTP {r.status_code} "
                                          f"{r.headers.get('content-type')} — is the doc shared 'Anyone with the link'?")
        except Exception as e:  # network / DNS / timeout
            frappe.logger("pos").warning(f"print template {kind}: {e}")
        last = frappe.cache().get_value(ck + ":last_good")
        out[kind], source[kind] = (last or ""), ("last_good" if last else "none")
    return {"templates": out, "source": source, "docs": PRINT_TEMPLATE_DOCS}

def redirect_to_pos():
    """301 to /pos, carrying the query string.

    `/pos2` was the live counter page until the three POS files collapsed into
    one, and every counter tablet has it bookmarked — deleting the route makes
    those bookmarks 404 on the till, mid-service. This is the one release where
    the old address still lands somewhere; re-point the bookmarks during a close
    and delete `www/pos2.*` after.

    Guests are not handled here: /pos does its own gate, so a bookmark opened on
    a signed-out tablet ends at /pos-login and comes back to /pos, not /pos2.
    """
    qs = frappe.request.query_string.decode() if frappe.request and frappe.request.query_string else ""
    frappe.local.flags.redirect_location = "/pos" + (("?" + qs) if qs else "")
    raise frappe.Redirect


def get_context(context):
    context.no_sidebar = True
    context.no_header = True
    context.no_footer = True

    if frappe.session.user == "Guest":
        # Send guests to the dedicated POS login screen (/pos-login), not an inline form.
        frappe.local.flags.redirect_location = "/pos-login?redirect=/pos"
        raise frappe.Redirect

    context.csrf_token = frappe.sessions.get_csrf_token()

    user = frappe.session.user
    full_name = frappe.get_value('User', user, 'full_name') or user.split('@')[0].title()
    context.current_user = user
    context.current_user_full = full_name

    # The set used to be written out here, and it had drifted: it named System
    # Manager, POS User, Sales User, Cashier and Administrator, and left out
    # Restaurant Manager and Restaurant Owner — the two roles above a cashier —
    # so a manager reached the till only through the Sales Invoice fallback
    # below. roles.POS_ROLES is the same list with those two in it.
    #
    # The fallback stays on purpose. Being locked out of the till mid-service is
    # a worse failure than an over-broad gate, and every endpoint the screen
    # then calls runs roles.require_pos() of its own, so what gets through here
    # can still do nothing it is not allowed to do.
    if not roles.has_any(roles.POS_ROLES) and not frappe.has_permission('Sales Invoice', 'create'):
        frappe.throw('You do not have permission to access POS', frappe.PermissionError)

    restaurant_name = branch.viewing()
    context.restaurant = None
    context.menu_groups = []
    context.items_flat = []
    context.tables = []
    context.service_charge_rate = 0

    if restaurant_name:
        restaurant = frappe.get_doc("Restaurant", restaurant_name)
        context.restaurant = restaurant
        context.service_charge_rate = float(restaurant.get("service_charge") or 0)
        # The till is the one screen that wants the add-on mappings; the template
        # reads them as a comma-joined data attribute.
        items = menu_items(restaurant_name, with_addons=True)
        for entry in items:
            entry["addons"] = ",".join(entry.get("addons") or [])
        items.sort(key=lambda x: (x.get("group", ""), x.get("rate", 0)))
        context.items_flat = items

        from itertools import groupby
        context.menu_groups = [{"name": key, "items": list(group)}
                               for key, group in groupby(items, key=lambda x: x["group"])]

    if not context.items_flat:
        # Fallback: load active items directly from Item master when no restaurant menu exists
        price_list = _get_pos_price_list()
        addon_map = {}
        if frappe.db.exists("DocType", "POS Item Addon"):
            for r in frappe.get_all("POS Item Addon", filters={"enabled": 1},
                                    fields=["item", "addon_item"]):
                addon_map.setdefault(r.item, []).append(r.addon_item)
        all_items = frappe.get_all("Item", filters={"disabled": 0}, fields=["name", "item_code", "item_name", "description", "image", "standard_rate", "item_group"])
        items = []
        for item_doc in all_items:
            item_code = item_doc.item_code or item_doc.name
            entry = {
                "code": item_code,
                "name": item_doc.item_name or item_code,
                "description": item_doc.description or "",
                "image": item_doc.image or "",
                "rate": _resolve_item_rate(item_code, item_doc.standard_rate, price_list),
                "group": item_doc.item_group or "General",
                "addons": ",".join(addon_map.get(item_code, [])),
            }
            items.append(entry)
        items.sort(key=lambda x: (x.get("group", ""), x.get("rate", 0)))
        context.items_flat = items
        from itertools import groupby
        groups = []
        for key, group in groupby(items, key=lambda x: x["group"]):
            groups.append({"name": key, "items": list(group)})
        context.menu_groups = groups

    _tables = tables.for_picker()
    for t in _tables:
        parts = t.name.split("-")
        t["short_name"] = parts[-1] if len(parts) > 1 else t.name
    context.tables = _tables

    profile_names = branch.accessible_pos_profiles(user)
    opening = frappe.db.get_value("POS Opening Entry",
        {"docstatus": 1, "status": "Open", "user": user,
         "pos_profile": ["in", profile_names]},
        ["name", "period_start_date"], as_dict=True)
    context.shift = {"status": "open"} if opening else None

    # A count, asked as a count. This pulled every draft order's name across the
    # wire to call len() on the list — and drafts are never cleared, so it grew
    # for the life of the site, on the page the counter opens every shift.
    context.ongoing_count = frappe.db.count("POS Order", {
        "docstatus": 0, "order_source": "Walk-in", "pos_invoice": "",
        branch.FIELD: restaurant_name})

    context.pos_profiles = frappe.get_all(
        "POS Profile", filters={"name": ["in", profile_names]},
        fields=["name", "warehouse", "company", "currency"]
    ) if profile_names else []
    context.payment_modes = frappe.get_all("Mode of Payment", fields=["name"], order_by="name asc")

    # Payment methods per POS Profile (for the native opening screen)
    import json as _json
    _pp = {}
    for _p in context.pos_profiles:
        _modes = frappe.get_all("POS Payment Method",
            filters={"parent": _p.name}, fields=["mode_of_payment"], order_by="idx")
        _pp[_p.name] = [m.mode_of_payment for m in _modes if m.mode_of_payment] or ["Cash"]
    context.pos_profile_payments_json = _json.dumps(_pp)
