"""Tour guides: their codes, their cashback, and the app they read it in.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
import secrets
import hashlib
from frappe.utils import now_datetime
from luuvcrm import roles

from luuvcrm.api.expenses import _ensure_select_option
from luuvcrm.api.wa import _normalize_wa_number
from luuvcrm.api import wa


# ─── Tour Guide Cashback ─────────────────────────────────────────────────────
# Tour guides (and the bus crew — driver/helper) who bring customers earn an
# automatic cashback: each partner's OWN percent of the bill, credited when the
# bill is paid (place_order / process_payment / pay_split). Balances are paid
# out later by a manager. Payouts are record-only — never a POS Cash Movement,
# for the same reason create_expense() stays out of the drawer: the POS's
# client-side close reconciliation and ERPNext's expected_amount both ignore
# drawer pay-outs, so writing one would make the close look short.
# Guides are external people — no Frappe Users. The /guide PWA authenticates
# with a Guide Token (Customer Token pattern) against a salted-hash password
# that is generated at quick-add and sent once over a wa.me invite.

GUIDE_DEFAULT_PERCENT = 5.0
GUIDE_MAX_PER_BILL = 3
GUIDE_APP_URL = "https://luuvgrand.com/guide"
# Invite codes gate NEW self-registrations over WhatsApp: without one, a stray
# "guide" in any customer message would create a Tour Guide record. Same
# unambiguous alphabet as the QR promo codes (no 0/O, 1/I/L).
GUIDE_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
GUIDE_CODE_RE = r"\bGD[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}\b"
# False → cashback base = food subtotal minus discount (the service charge is a
# tax row, not customer spend). True → base = invoice grand total.
GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE = False
@frappe.whitelist()
def setup_tour_guides():
    """One-time setup: Tour Guide + Guide Token + Guide Cashback Txn DocTypes.
    Run: bench --site <site> execute luuvcrm.api.setup_tour_guides"""
    frappe.only_for("System Manager")
    created = []

    if not frappe.db.exists("DocType", "Tour Guide"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Tour Guide", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "guide_name", "label": "Guide Name", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "whatsapp", "label": "WhatsApp", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "role", "label": "Role", "fieldtype": "Select", "options": "Guide\nDriver\nHelper", "default": "Guide", "in_list_view": 1},
                {"fieldname": "cashback_percent", "label": "Cashback %", "fieldtype": "Percent", "default": GUIDE_DEFAULT_PERCENT},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Invited\nActive", "default": "Invited", "in_list_view": 1},
                {"fieldname": "password_hash", "label": "Password Hash", "fieldtype": "Data"},
                {"fieldname": "cashback_balance", "label": "Cashback Balance", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_earned", "label": "Total Earned", "fieldtype": "Currency", "default": 0},
                {"fieldname": "total_paid", "label": "Total Paid", "fieldtype": "Currency", "default": 0},
                {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
                {"fieldname": "notes", "label": "Notes", "fieldtype": "Small Text"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1, "write": 1, "create": 1},
            ],
        }).insert()
        created.append("Tour Guide")

    if not frappe.db.exists("DocType", "Guide Token"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Token", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "token", "label": "Token", "fieldtype": "Data", "reqd": 1, "unique": 1},
                {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1},
                {"fieldname": "created", "label": "Created", "fieldtype": "Datetime", "reqd": 1},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert()
        created.append("Guide Token")

    if not frappe.db.exists("DocType", "Guide Cashback Txn"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Cashback Txn", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "guide", "label": "Guide", "fieldtype": "Link", "options": "Tour Guide", "reqd": 1, "in_list_view": 1},
                {"fieldname": "type", "label": "Type", "fieldtype": "Select", "options": "Earn\nPayout", "reqd": 1, "in_list_view": 1},
                {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1, "in_list_view": 1},
                {"fieldname": "reference_order", "label": "Reference Order", "fieldtype": "Data"},
                {"fieldname": "reference_invoice", "label": "Reference Invoice", "fieldtype": "Data"},
                {"fieldname": "bill_total", "label": "Bill Total (base)", "fieldtype": "Currency"},
                {"fieldname": "percent_used", "label": "Percent Used", "fieldtype": "Percent"},
                {"fieldname": "note", "label": "Note", "fieldtype": "Small Text"},
                {"fieldname": "paid_by", "label": "Paid By", "fieldtype": "Link", "options": "User"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1},
            ],
        }).insert()
        created.append("Guide Cashback Txn")

    if not frappe.db.exists("DocType", "Guide Invite Code"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Invite Code", "module": "Zeloura", "custom": 1,
            "fields": [
                {"fieldname": "code", "label": "Code", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "New\nUsed", "default": "New", "in_list_view": 1},
                {"fieldname": "used_by", "label": "Used By", "fieldtype": "Link", "options": "Tour Guide", "in_list_view": 1},
                {"fieldname": "used_on", "label": "Used On", "fieldtype": "Datetime"},
                {"fieldname": "created_by_user", "label": "Created By", "fieldtype": "Link", "options": "User"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Cashier", "read": 1},
            ],
        }).insert()
        created.append("Guide Invite Code")

    # "Redeem" (guide pays their own meal from balance) was added after the
    # doctype first shipped — register the option on existing installs too.
    if frappe.db.exists("DocType", "Guide Cashback Txn"):
        _ensure_select_option("Guide Cashback Txn", "type", "Redeem")

    # Global default % — manager-editable (Loyalty Settings pattern); replaces
    # the hardcoded GUIDE_DEFAULT_PERCENT for new guides and as the fallback.
    if not frappe.db.exists("DocType", "Guide Settings"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Guide Settings", "module": "Zeloura",
            "custom": 1, "issingle": 1,
            "fields": [
                {"fieldname": "default_percent", "label": "Default Cashback %",
                 "fieldtype": "Percent", "default": GUIDE_DEFAULT_PERCENT},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert()
        created.append("Guide Settings")
    if frappe.db.exists("DocType", "Guide Settings") \
            and not frappe.db.exists("Guide Settings", "Guide Settings"):
        s = frappe.get_doc({"doctype": "Guide Settings"})
        s.flags.ignore_permissions = True
        s.insert()

    # Item-wise commission override: an Item's guide_commission_percent beats
    # the guide's own percent for that line (most-specific wins).
    if not frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "guide_commission_percent"}):
        cf = frappe.get_doc({
            "doctype": "Custom Field", "dt": "Item",
            "fieldname": "guide_commission_percent", "label": "Guide Commission %",
            "fieldtype": "Percent", "insert_after": "standard_rate",
        })
        cf.flags.ignore_permissions = True
        cf.insert()
        created.append("Item.guide_commission_percent")

    return {"status": "created" if created else "already_exists", "created": created}
def _guide_default_percent():
    """Manager-set global cashback % (Guide Settings single), else the code default."""
    try:
        if frappe.db.exists("DocType", "Guide Settings"):
            v = frappe.db.get_single_value("Guide Settings", "default_percent")
            if v is not None and float(v) > 0:
                return float(v)
    except Exception:
        pass
    return GUIDE_DEFAULT_PERCENT
def _hash_guide_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + (pw or '')).encode()).hexdigest()}"
def _verify_guide_pw(pw, stored):
    if not stored or "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return secrets.compare_digest(_hash_guide_pw(pw, salt), stored)
def _gen_guide_password():
    """8 chars from an unambiguous alphabet (no 0/O, 1/I/L) — typed off a
    WhatsApp message on a phone."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))
def _gen_guide_code():
    for _ in range(50):
        code = "GD" + "".join(secrets.choice(GUIDE_CODE_ALPHABET) for _ in range(6))
        if not frappe.db.exists("Guide Invite Code", {"code": code}):
            return code
    return "GD" + secrets.token_hex(3).upper()
@frappe.whitelist(methods=["POST"])
def create_guide_codes(count=5):
    """Mint partner invite codes (manager). Each code registers exactly one new
    guide over WhatsApp; staff hand them out in person or share the wa.me link."""
    roles.require_manager()
    count = max(1, min(int(count or 5), 50))
    codes = []
    for _ in range(count):
        code = _gen_guide_code()
        frappe.get_doc({
            "doctype": "Guide Invite Code", "code": code, "status": "New",
            "created_by_user": frappe.session.user,
        }).insert(ignore_permissions=True)
        codes.append(code)
    frappe.db.commit()
    return {"ok": True, "codes": codes}
@frappe.whitelist()
def get_guide_codes():
    roles.require_manager()
    fresh = frappe.get_all("Guide Invite Code", filters={"status": "New"},
        fields=["code", "creation"], order_by="creation desc", limit=50)
    used = frappe.get_all("Guide Invite Code", filters={"status": "Used"},
        fields=["code", "used_by", "used_on"], order_by="used_on desc", limit=20)
    for u in used:
        u["guide_name"] = (frappe.db.get_value("Tour Guide", u["used_by"],
                           "guide_name") or "") if u.get("used_by") else ""
        u["used_on"] = str(u.get("used_on") or "")[:16]
    for f in fresh:
        f["creation"] = str(f["creation"])[:10]
    return {"fresh": fresh, "used": used}
def _guide_invite_text(token, percent, guide_name=""):
    """Magic-link invite: tapping the link logs the guide straight in (the token
    IS the session) — no password to type. WhatsApp possession is the auth root."""
    hello = f"Hi {guide_name}!" if guide_name else "Hi!"
    return (f"{hello} Welcome to Luuv Fryxo's partner program. "
            f"You earn {percent:g}% cashback on every bill you bring us. "
            f"Tap your personal link to open your cashback account:\n"
            f"{GUIDE_APP_URL}?t={token}")
# ── Cashier-facing (POS session) ──

@frappe.whitelist()
def guide_search(mobile=None):
    """Find a Tour Guide by WhatsApp number for the POS payment dialog."""
    roles.require_pos()
    number = _normalize_wa_number(mobile)
    name = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if not name:
        return {"found": False, "whatsapp": number}
    g = frappe.db.get_value("Tour Guide", name,
        ["guide_name", "role", "cashback_percent", "status", "cashback_balance"], as_dict=True)
    return {"found": True, "name": name, "guide_name": g.guide_name or "",
            "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
            "status": g.status, "whatsapp": number,
            "cashback_balance": float(g.cashback_balance or 0)}
@frappe.whitelist(methods=["POST"])
def guide_quick_add(mobile=None, name="", role="Guide", percent=None):
    """Create an Invited guide from the POS. Returns the generated password and
    ready-made invite text EXACTLY ONCE so the client can open the wa.me link —
    only the salted hash is stored."""
    roles.require_pos()
    number = _normalize_wa_number(mobile)
    existing = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if existing:
        g = frappe.db.get_value("Tour Guide", existing,
            ["guide_name", "role", "cashback_percent", "status"], as_dict=True)
        return {"found": True, "name": existing, "guide_name": g.guide_name or "",
                "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
                "status": g.status, "whatsapp": number}
    if role not in ("Guide", "Driver", "Helper"):
        role = "Guide"
    pct = float(percent) if percent not in (None, "") else _guide_default_percent()
    if pct < 0 or pct > 50:
        frappe.throw(_("Cashback percent must be between 0 and 50"))
    guide = frappe.get_doc({
        "doctype": "Tour Guide",
        "guide_name": (name or "").strip(),
        "whatsapp": number,
        "role": role,
        "cashback_percent": pct,
        "status": "Invited",
        # random unused hash — login is via the magic-link token, not a password
        "password_hash": _hash_guide_pw(_gen_guide_password()),
        "created_by_user": frappe.session.user,
    })
    guide.insert(ignore_permissions=True)
    token = _get_or_create_guide_token(guide.name)
    frappe.db.commit()
    return {"found": True, "created": True, "name": guide.name,
            "guide_name": guide.guide_name, "role": role, "cashback_percent": pct,
            "status": "Invited", "whatsapp": number,
            "invite_text": _guide_invite_text(token, pct, guide.guide_name)}
# ── Settlement hook ──

def _notify_guide_earning(guide, amount, order_name):
    """Best-effort WhatsApp to the guide the moment a bill credits them: this
    bill's cashback, today's running total, and the full balance. Rides the
    Cloud API text send — delivery only works inside Meta's 24-hour service
    window (opened by the guide's OWN messages), so failures are expected and
    swallowed; the payment must never notice."""
    try:
        today_total = frappe.db.sql("""
            select coalesce(sum(amount),0) from `tabGuide Cashback Txn`
            where guide=%s and type='Earn' and creation >= %s
        """, (guide.name, frappe.utils.today()))[0][0]
        token = _get_or_create_guide_token(guide.name)
        hello = f"Hi {guide.guide_name}!" if (guide.guide_name or "").strip() else "Hi!"
        wa._send_wa_reply(guide.whatsapp,
            f"🧭 {hello} A bill you brought us was just paid.\n\n"
            f"💰 This bill: Rs {amount:,.2f}\n"
            f"📅 Today so far: Rs {float(today_total):,.2f}\n"
            f"🏦 Your balance: Rs {float(guide.cashback_balance or 0):,.2f}\n\n"
            f"Details: {GUIDE_APP_URL}?t={token}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Guide WA notify")
def _parse_guide_redeem(redeem_param, bill_total):
    """Validate a {guide, amount} balance-redemption request (guide eats and pays
    from their cashback). THROWS on any problem — a redemption the guide believes
    covered the bill must never silently no-op. Returns (guide_name, amount)."""
    if not redeem_param:
        return None, 0.0
    req = frappe.parse_json(redeem_param) if isinstance(redeem_param, str) else redeem_param
    gname = (req or {}).get("guide")
    amt = round(float((req or {}).get("amount") or 0), 2)
    if not gname or amt <= 0:
        return None, 0.0
    if not frappe.db.exists("Tour Guide", gname):
        frappe.throw(_("Guide not found for balance redemption"))
    balance = float(frappe.db.get_value("Tour Guide", gname, "cashback_balance") or 0)
    if amt > balance + 0.005:
        frappe.throw(_("Guide balance is only Rs {0:,.2f}").format(balance))
    if amt > float(bill_total or 0) + 0.005:
        frappe.throw(_("Redemption can't exceed the bill total"))
    return gname, amt
def _finalize_guide_redeem(gname, amount, order_name, invoice_name):
    """Deduct the redeemed amount from the guide's balance + log a Redeem txn.
    Called after invoice submit; the invoice already carries the amount as a
    Net Total discount, so the payment rows only cover what the customer paid."""
    if not gname or amount <= 0:
        return
    _ensure_select_option("Guide Cashback Txn", "type", "Redeem")
    guide = frappe.get_doc("Tour Guide", gname)
    guide.cashback_balance = (guide.cashback_balance or 0) - amount
    guide.save(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Guide Cashback Txn",
        "guide": gname,
        "type": "Redeem",
        "amount": amount,
        "reference_order": order_name or "",
        "reference_invoice": invoice_name or "",
        "note": "Bill paid from cashback balance",
    }).insert(ignore_permissions=True)
    try:
        token = _get_or_create_guide_token(guide.name)
        wa._send_wa_reply(guide.whatsapp,
            f"🍔 Rs {amount:,.2f} was paid from your Luuv Fryxo cashback balance"
            f"{(' for bill ' + order_name) if order_name else ''}.\n\n"
            f"🏦 Remaining balance: Rs {float(guide.cashback_balance or 0):,.2f}\n\n"
            f"Details: {GUIDE_APP_URL}?t={token}")
    except Exception:
        pass
    frappe.db.commit()
def _credit_guide_cashback(guides_param, base_amount, order_name, invoice_name, items=None):
    """Credit each attached partner their cashback and log an Earn txn.
    Idempotent per (guide, invoice). Never raises — a cashback problem must not
    fail the payment it rides on.

    Rate hierarchy (most specific wins): an Item's guide_commission_percent
    custom field overrides the guide's own percent for that line; the guide's
    percent (defaulted from Guide Settings at creation) covers the rest.
    base_amount = subtotal − discount, so the per-item math is scaled by
    base/subtotal and bill discounts shrink commissions proportionally."""
    try:
        if not guides_param or not frappe.db.exists("DocType", "Guide Cashback Txn"):
            return
        guides = frappe.parse_json(guides_param) if isinstance(guides_param, str) else guides_param
        base = float(base_amount or 0)
        if not guides or base <= 0:
            return
        items = items or []
        subtotal = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0) for i in items)
        item_pct = {}
        if items and frappe.db.has_column("Item", "guide_commission_percent"):
            for i in items:
                code = i.get("item")
                if code and code not in item_pct:
                    item_pct[code] = float(frappe.db.get_value(
                        "Item", code, "guide_commission_percent") or 0)
        for gname in list(dict.fromkeys(guides))[:GUIDE_MAX_PER_BILL]:
            if not frappe.db.exists("Tour Guide", gname):
                continue
            if invoice_name and frappe.db.exists("Guide Cashback Txn",
                    {"guide": gname, "type": "Earn", "reference_invoice": invoice_name}):
                continue  # client retry — this bill already credited this guide
            if invoice_name and frappe.db.exists("Guide Cashback Txn",
                    {"guide": gname, "type": "Redeem", "reference_invoice": invoice_name}):
                continue  # guide paid this bill FROM their balance — no earning on it
            guide = frappe.get_doc("Tour Guide", gname)
            pct = float(guide.cashback_percent or 0)
            if items and subtotal > 0:
                raw = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0)
                          * ((item_pct.get(i.get("item")) or pct)) / 100.0
                          for i in items)
                amount = round(raw * (base / subtotal), 2)
            else:
                amount = round(base * pct / 100, 2)
            if amount <= 0:
                continue
            eff_pct = round(amount * 100.0 / base, 2) if base > 0 else pct
            guide.cashback_balance = (guide.cashback_balance or 0) + amount
            guide.total_earned = (guide.total_earned or 0) + amount
            guide.save(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Guide Cashback Txn",
                "guide": gname,
                "type": "Earn",
                "amount": amount,
                "reference_order": order_name or "",
                "reference_invoice": invoice_name or "",
                "bill_total": base,
                "percent_used": eff_pct,
            }).insert(ignore_permissions=True)
            _notify_guide_earning(guide, amount, order_name)
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Guide Cashback")
# ── Guide app (/guide PWA — token auth, no Frappe Users) ──

def _get_or_create_guide_token(guide_name):
    # 90 days, not the customer scheme's 7: the token doubles as the magic-link
    # login (guides may only visit monthly); an expired one just means messaging
    # GUIDE on WhatsApp again for a fresh link.
    frappe.db.sql("DELETE FROM `tabGuide Token` WHERE created < NOW() - INTERVAL 90 DAY")
    frappe.db.commit()
    existing = frappe.db.get_value("Guide Token", {"guide": guide_name}, "token")
    if existing:
        return existing
    token = secrets.token_hex(16)
    frappe.get_doc({
        "doctype": "Guide Token", "token": token,
        "guide": guide_name, "created": now_datetime(),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return token
def _guide_from_token(token):
    if not token:
        return None
    return frappe.db.get_value("Guide Token", {"token": str(token).strip()}, "guide")
@frappe.whitelist(allow_guest=True, methods=["POST"])
def guide_login(mobile=None, password=None):
    number = _normalize_wa_number(mobile)
    name = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
    if not name:
        return {"success": False, "error": "No guide account for this number"}
    stored = frappe.db.get_value("Tour Guide", name, "password_hash")
    if not _verify_guide_pw((password or "").strip(), stored):
        return {"success": False, "error": "Wrong password"}
    guide = frappe.get_doc("Tour Guide", name)
    if guide.status != "Active":
        guide.db_set("status", "Active")
    return {"success": True, "token": _get_or_create_guide_token(name),
            "guide_name": guide.guide_name or "", "role": guide.role or "Guide",
            "cashback_percent": float(guide.cashback_percent or 0),
            "needs_profile": not (guide.guide_name or "").strip()}
@frappe.whitelist(allow_guest=True)
def guide_dashboard(token=None):
    name = _guide_from_token(token)
    if not name:
        return {"success": False, "error": "Login required"}
    g = frappe.get_doc("Tour Guide", name)
    txns = frappe.get_all("Guide Cashback Txn",
        filters={"guide": name},
        fields=["type", "amount", "reference_order", "bill_total", "percent_used",
                "note", "creation"],
        order_by="creation desc", limit=50)
    for t in txns:
        t["creation"] = str(t["creation"])[:16]
    return {"success": True,
            "guide_name": g.guide_name or "", "whatsapp": g.whatsapp,
            "role": g.role or "Guide", "cashback_percent": float(g.cashback_percent or 0),
            "cashback_balance": float(g.cashback_balance or 0),
            "total_earned": float(g.total_earned or 0),
            "total_paid": float(g.total_paid or 0),
            "earnings": [t for t in txns if t["type"] == "Earn"],
            "payouts": [t for t in txns if t["type"] != "Earn"]}
@frappe.whitelist(allow_guest=True, methods=["POST"])
def guide_update_profile(token=None, guide_name=None, notes=None):
    name = _guide_from_token(token)
    if not name:
        return {"success": False, "error": "Login required"}
    guide = frappe.get_doc("Tour Guide", name)
    if guide_name is not None and str(guide_name).strip():
        guide.guide_name = str(guide_name).strip()
    if notes is not None:
        guide.notes = str(notes).strip()
    guide.save(ignore_permissions=True)
    frappe.db.commit()
    return {"success": True, "guide_name": guide.guide_name or ""}
# ── Manager (list, payout, edit) ──



@frappe.whitelist()
def list_guides():
    roles.require_manager()
    guides = frappe.get_all("Tour Guide",
        fields=["name", "guide_name", "whatsapp", "role", "cashback_percent",
                "status", "cashback_balance", "total_earned", "total_paid"],
        order_by="cashback_balance desc, guide_name asc")
    for g in guides:
        for f in ("cashback_percent", "cashback_balance", "total_earned", "total_paid"):
            g[f] = float(g[f] or 0)
    return {"guides": guides, "default_percent": _guide_default_percent()}
@frappe.whitelist(methods=["POST"])
def update_guide_settings(percent=None):
    """Set the global default cashback % (used for every newly registered guide;
    existing guides keep their own percent — edit those per guide)."""
    roles.require_manager()
    pct = float(percent)
    if pct < 0 or pct > 50:
        frappe.throw(_("Cashback percent must be between 0 and 50"))
    doc = frappe.get_doc("Guide Settings")
    doc.default_percent = pct
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "default_percent": pct}
@frappe.whitelist()
def guide_detail(guide=None):
    roles.require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    txns = frappe.get_all("Guide Cashback Txn",
        filters={"guide": g.name},
        fields=["type", "amount", "reference_order", "bill_total", "percent_used",
                "note", "paid_by", "creation"],
        order_by="creation desc", limit=100)
    for t in txns:
        t["creation"] = str(t["creation"])[:16]
    return {"name": g.name, "guide_name": g.guide_name or "", "whatsapp": g.whatsapp,
            "role": g.role or "Guide", "status": g.status,
            "cashback_percent": float(g.cashback_percent or 0),
            "cashback_balance": float(g.cashback_balance or 0),
            "total_earned": float(g.total_earned or 0),
            "total_paid": float(g.total_paid or 0),
            "notes": g.notes or "", "txns": txns}
@frappe.whitelist(methods=["POST"])
def record_guide_payout(guide=None, amount=None, note=""):
    """Pay a guide part/all of their balance. RECORD-ONLY: this deliberately
    does NOT write a POS Cash Movement — the POS's client-side close recon and
    ERPNext's expected_amount both ignore drawer pay-outs (same rationale as
    create_expense), so the drawer is left untouched and the payout lives only
    in the guide's ledger."""
    roles.require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    amt = float(amount or 0)
    balance = float(g.cashback_balance or 0)
    if amt <= 0:
        frappe.throw(_("Enter a payout amount above zero"))
    if amt > balance + 0.005:
        frappe.throw(_("Payout exceeds balance (Rs {0:,.2f})").format(balance))
    g.cashback_balance = balance - amt
    g.total_paid = (g.total_paid or 0) + amt
    g.save(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Guide Cashback Txn",
        "guide": g.name,
        "type": "Payout",
        "amount": amt,
        "note": (note or "").strip(),
        "paid_by": frappe.session.user,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "cashback_balance": float(g.cashback_balance or 0),
            "total_paid": float(g.total_paid or 0)}
@frappe.whitelist(methods=["POST"])
def update_guide(guide=None, guide_name=None, role=None, percent=None, status=None):
    roles.require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    if guide_name is not None:
        g.guide_name = str(guide_name).strip()
    if role in ("Guide", "Driver", "Helper"):
        g.role = role
    if percent not in (None, ""):
        pct = float(percent)
        if pct < 0 or pct > 50:
            frappe.throw(_("Cashback percent must be between 0 and 50"))
        g.cashback_percent = pct
    if status in ("Invited", "Active"):
        g.status = status
    g.save(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True}
@frappe.whitelist(methods=["POST"])
def reset_guide_password(guide=None):
    """Issue a fresh magic-link login for the guide (old links stop working) and
    return the wa.me re-invite text. Name kept for the manager UI's old call."""
    roles.require_manager()
    g = frappe.get_doc("Tour Guide", guide)
    frappe.db.delete("Guide Token", {"guide": g.name})
    token = _get_or_create_guide_token(g.name)
    frappe.db.commit()
    return {"ok": True, "whatsapp": g.whatsapp,
            "invite_text": _guide_invite_text(token,
                float(g.cashback_percent or 0), g.guide_name or "")}
