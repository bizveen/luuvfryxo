"""Customer accounts: registering, the PIN, the token, the profile, and SMS OTP.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import html
import re

import frappe

import secrets
import hashlib
from frappe.utils import now_datetime

from luuvcrm import settings
from luuvcrm.customer_phone import phone_variants
from luuvcrm.api.orders import lines_by_order
from luuvcrm.api.wa import _wa_number
from luuvcrm.api import sms, wa


# ─── Customer Account Setup ──────────────────────────────

@frappe.whitelist()
def setup_customer_accounts():
    """Report whether Customer Account and Customer Token are on this site.

    **It used to delete both DocTypes and rebuild them** — with `custom: 1`, on
    every call, from a Python dict. Two things wrong with that, and they are
    different sizes:

    - Dropping `Customer Account` drops every customer's account and PIN, and
      dropping `Customer Token` signs out every customer holding a session. A
      whitelisted endpoint any System Manager could call, with no confirmation.
    - `custom: 1` takes the DocType straight back out of `bench migrate`'s path,
      undoing the conversion Phase 3 exists for. That is how it was found: the
      test suite called this to "ensure" the DocTypes existed, and
      `test_setup.test_the_site_agrees` failed the moment it did.

    Both now ship as JSON under `luuvcrm/zeloura/doctype/`, so creating them is
    Frappe's job — `install_app` and `bench migrate` sync them. This says what is
    there and what is not, and changes nothing.
    """
    frappe.only_for("System Manager")
    wanted = ["Customer Account", "Customer Token"]
    missing = [d for d in wanted if not frappe.db.exists("DocType", d)]
    return {
        "status": "missing" if missing else "present",
        "present": [d for d in wanted if d not in missing],
        "missing": missing,
        "hint": ("run `bench --site <site> migrate` — they come from "
                 "luuvcrm/zeloura/doctype/") if missing else "",
    }


# ─── Customer Account API ─────────────────────────────────

def _get_or_create_customer_token(mobile):
    # Clean existing expired tokens (older than 7 days)
    frappe.db.sql("DELETE FROM `tabCustomer Token` WHERE created < NOW() - INTERVAL 7 DAY")
    frappe.db.commit()
    # Check for existing valid token
    existing = frappe.db.get_value("Customer Token", {"mobile": mobile}, "token")
    if existing:
        return existing
    # Create new token
    token = secrets.token_hex(16)
    ct = frappe.get_doc({
        "doctype": "Customer Token",
        "token": token,
        "mobile": mobile,
        "created": now_datetime(),
    })
    ct.flags.ignore_permissions = True
    ct.insert()
    frappe.db.commit()
    return token
#: The customer PWA shows a recent history, not an archive. The page may ask
#: for more with `limit`; the ceiling is what stops a request asking for a
#: whole table on a guest token.
CUSTOMER_ORDER_PAGE = 20
CUSTOMER_ORDER_MAX = 100
CUSTOMER_FEEDBACK_SUBJECT = "Luuv customer order feedback"

@frappe.whitelist(allow_guest=True)
def customer_create_or_login():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    name = data.get("name", "").strip()

    if not mobile or len(mobile) < 10:
        return {"success": False, "error": "Valid phone number required"}

    existing = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")

    if existing:
        # Return that account exists — PIN verification needed
        account = frappe.get_doc("Customer Account", existing)
        return {"success": True, "exists": True, "name": account.customer_name or ""}

    # New account — set PIN in next step
    frappe.db.set_value("Customer Account", None, {})  # dummy
    return {"success": True, "exists": False}
@frappe.whitelist(allow_guest=True)
def customer_set_pin():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    pin = data.get("pin", "").strip()
    name = data.get("name", "").strip()

    if not mobile or len(mobile) < 10:
        return {"success": False, "error": "Valid phone required"}
    if not pin or len(pin) != 4 or not pin.isdigit():
        return {"success": False, "error": "PIN must be 4 digits"}

    existing = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")
    if existing:
        return {"success": False, "error": "Account already exists"}

    # A new account is created only after this phone has completed the SMS OTP.
    # Existing accounts never return to this path; they sign in with their PIN.
    cache = frappe.cache()
    if not cache.get_value(f"sms_verified:{mobile}", expires=True):
        return {"success": False, "error": "Confirm the verification code first"}

    account = frappe.get_doc({
        "doctype": "Customer Account",
        "mobile": mobile,
        "customer_name": name,
        "pin": _hash_pin(pin),
        "last_login": now_datetime(),
    })
    account.flags.ignore_permissions = True
    account.insert(ignore_permissions=True)
    frappe.db.commit()

    # The confirmation is a one-time registration gate, not a reusable grant.
    cache.delete_value(f"sms_verified:{mobile}")

    token = _get_or_create_customer_token(mobile)
    return {"success": True, "token": token, "name": name}
# ── customer PIN ──
# A 4-digit PIN is a 10,000-key space, so the lockout below is what actually
# protects the account; the hash protects it if the table ever leaks. The guide
# login has done both since it was written (_hash_guide_pw) — this brings the
# customer path up to the same standard, reusing the same salted scheme.
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SEC = 900
def _hash_pin(pin, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + (pin or '')).encode()).hexdigest()}"
def _verify_pin(pin, stored):
    """True if the PIN matches. Second return value says the stored form is a
    legacy plaintext PIN and should be upgraded to a hash by the caller."""
    if not stored:
        return False, False
    if "$" in stored:
        salt = stored.split("$", 1)[0]
        return secrets.compare_digest(_hash_pin(pin, salt), stored), False
    # Pre-hash account: constant-time compare, then upgrade on the way through
    # so plaintext PINs drain out of the table as people sign in.
    return secrets.compare_digest(str(stored), str(pin)), True
def _pin_attempts_key(mobile):
    return f"lf_pin_fail:{mobile}"
def _pin_fails(mobile):
    """Failed attempts for this number.

    expires=True matters: without it get_value() writes the miss into
    frappe.local.cache, while a set_value() carrying expires_in_sec deliberately
    does not update that dict — so a read-after-write inside one request keeps
    seeing the stale None and the counter never leaves 1.
    """
    return int(frappe.cache().get_value(_pin_attempts_key(mobile), expires=True) or 0)
def _pin_locked(mobile):
    """Fails open when the cache is unreachable. A restaurant losing redis must
    not also lose every customer login; the hash still protects a leaked table."""
    return _pin_fails(mobile) >= PIN_MAX_ATTEMPTS
def _pin_note_failure(mobile):
    fails = _pin_fails(mobile) + 1
    frappe.cache().set_value(_pin_attempts_key(mobile), fails, expires_in_sec=PIN_LOCKOUT_SEC)
    return fails
@frappe.whitelist(allow_guest=True)
def customer_verify_pin():
    data = frappe.local.form_dict
    mobile = data.get("mobile", "").strip()
    pin = data.get("pin", "").strip()

    if not mobile or not pin:
        return {"success": False, "error": "Phone and PIN required"}

    if _pin_locked(mobile):
        return {"success": False, "error": "Too many wrong PINs — try again in 15 minutes"}

    # One account per number is the invariant; ask for one row, not the set.
    accounts = frappe.get_all("Customer Account", filters={"mobile": mobile},
                              fields=["name", "customer_name"], limit_page_length=1)
    if not accounts:
        # Same shape and cost as a wrong PIN: do not confirm which numbers exist.
        _pin_note_failure(mobile)
        return {"success": False, "error": "Wrong PIN"}

    account = accounts[0]
    stored = frappe.db.get_value("Customer Account", account["name"], "pin")
    ok, needs_upgrade = _verify_pin(pin, stored)
    if not ok:
        _pin_note_failure(mobile)
        return {"success": False, "error": "Wrong PIN"}
    if needs_upgrade:
        frappe.db.set_value("Customer Account", account["name"], "pin", _hash_pin(pin))
    frappe.cache().delete_value(_pin_attempts_key(mobile))

    # Update last login
    frappe.db.set_value("Customer Account", account["name"], "last_login", now_datetime())

    token = _get_or_create_customer_token(mobile)
    return {"success": True, "token": token, "name": account["customer_name"]}
@frappe.whitelist(allow_guest=True)
def customer_get_orders():
    data = frappe.local.form_dict
    token = data.get("token", "").strip()

    # The token is the ONLY way in. There used to be a "backward compatibility"
    # branch that accepted a bare `mobile` when no token was sent and returned
    # every order for that number — items, totals, table, times — to anyone who
    # asked. Local mobile numbers are a 10-digit space with a known prefix, so
    # that was enumerable: a personal-data breach, not a convenience.
    # /cus, /myorders and /my_orders all send the token, so nothing loses access.
    mobile = _mobile_from_token(token)
    if not mobile:
        return {"success": False, "error": "Login required"}

    # Fetch all orders for this mobile
    from urllib.parse import quote
    _fields = ["name", "customer_name", "restaurant_table", "grand_total",
               "kitchen_status", "docstatus", "creation", "modified", "order_source",
               "whatsapp_status"]
    if frappe.db.has_column("POS Order", "scheduled_time"):
        _fields.append("scheduled_time")
    # Every order this number has ever placed, newest first — and then a full
    # document built for each. A regular of two years is hundreds of rows and
    # hundreds of documents, on a guest request. The PWA shows a recent history,
    # so it asks for one; `limit` lets it ask for more without another endpoint.
    try:
        limit = max(1, min(int(data.get("limit") or CUSTOMER_ORDER_PAGE), CUSTOMER_ORDER_MAX))
    except Exception:
        limit = CUSTOMER_ORDER_PAGE
    orders = frappe.get_all("POS Order",
        filters={"mobile": ["in", phone_variants(mobile)]},
        fields=_fields,
        order_by="creation desc",
        limit_page_length=limit,
    )

    order_names = [o.name for o in orders]
    lines = lines_by_order(order_names)
    feedback_rows = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": "POS Order",
            "reference_name": ["in", order_names],
            "subject": CUSTOMER_FEEDBACK_SUBJECT,
        },
        fields=["reference_name", "content"],
        limit_page_length=0,
    ) if order_names else []
    feedback_ratings = {}
    for row in feedback_rows:
        match = re.search(r"Customer rating:\s*([1-5])/5", row.content or "")
        feedback_ratings[row.reference_name] = int(match.group(1)) if match else 0
    result = []
    for o in orders:
        items = [{"item": i.item, "item_name": i.item_name or i.item, "qty": i.qty, "rate": i.rate}
                 for i in lines.get(o.name, [])]
        awaiting = (o.get("whatsapp_status") or "") == "Awaiting Confirmation"
        result.append({
            "name": o.name,
            "customer_name": o.customer_name,
            "table": o.restaurant_table or "",
            "grand_total": o.grand_total or 0,
            "status": o.kitchen_status or "Pending",
            "docstatus": o.docstatus,
            "placed_at": str(o.creation),
            "completed_at": str(o.modified) if o.docstatus == 1 else "",
            "time_ago": frappe.utils.pretty_date(o.creation),
            "items": items,
            "item_count": len(items),
            "wa_status": o.get("whatsapp_status") or "",
            "scheduled_time": str(o.get("scheduled_time") or ""),
            "confirm_link": ("https://wa.me/" + wa.wa_number() + "?text="
                             + quote(f"Confirming my order #{o.name} ✅")) if awaiting else "",
            "feedback_submitted": o.name in feedback_ratings,
            "feedback_rating": feedback_ratings.get(o.name, 0),
        })

    return {"success": True, "orders": result, "mobile": mobile}


@frappe.whitelist(allow_guest=True)
def customer_account_summary():
    """Small, exact order totals for the signed-in customer's account tab."""
    token = str(frappe.local.form_dict.get("token") or "").strip()
    mobile = _mobile_from_token(token)
    if not mobile:
        return {"success": False, "error": "Login required"}
    variants = phone_variants(mobile)
    placeholders = ", ".join(["%s"] * len(variants))
    rows = frappe.db.sql(
        f"""
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(SUM(CASE
                WHEN docstatus = 0
                 AND IFNULL(kitchen_status, 'Pending') NOT IN ('Served', 'Cancelled')
                THEN 1 ELSE 0 END), 0) AS active_orders
        FROM `tabPOS Order`
        WHERE mobile IN ({placeholders})
        """,
        tuple(variants),
        as_dict=True,
    )
    row = rows[0] if rows else frappe._dict()
    return {
        "success": True,
        "total_orders": int(row.get("total_orders") or 0),
        "active_orders": int(row.get("active_orders") or 0),
    }
# ─── Customer profile (name / address / area — one account per number) ─────

def _install_customer_profile_fields():
    """Add address + area to Customer Account if an old site has not got them.

    They are declared in `customer_account.json` now, so a synced site already
    has them and this does nothing. That matters: the `dt.save()` below is what
    marked the DocType custom on a site that was missing them, which quietly
    took it out of `bench migrate`'s path again.
    """
    dt = frappe.get_doc("DocType", "Customer Account")
    have = {f.fieldname for f in dt.fields}
    added = []
    for fn, label, ftype in [("address", "Address", "Small Text"), ("area", "Area", "Data")]:
        if fn not in have:
            dt.append("fields", {"fieldname": fn, "label": label, "fieldtype": ftype})
            added.append(fn)
    if added:
        dt.flags.ignore_permissions = True
        dt.save(ignore_permissions=True)
        frappe.db.commit()
    return {"added": added}
@frappe.whitelist()
def setup_customer_profile_fields():
    """Schema setup over HTTP. Admin only; the work is in _install_customer_profile_fields."""
    frappe.only_for("System Manager")
    return _install_customer_profile_fields()
def _customer_mobile(data=None):
    """The signed-in customer's mobile, from their token. "" when not signed in.

    Loyalty endpoints used to take the mobile straight from the request while
    being allow_guest, so anyone who knew a phone number could read that
    customer's balance and referral network — and, through loyalty_redeem, spend
    the balance. Same enumeration hole as the customer_get_orders fallback closed
    in Phase 1 (S3), except this one moved money.
    """
    data = data if data is not None else frappe.local.form_dict
    return _mobile_from_token((data.get("token") or "").strip()) or ""
CUSTOMER_TOKEN_DAYS = 7
def _mobile_from_token(token):
    """The mobile behind a customer token, or None.

    Enforces the 7-day life here rather than trusting the lazy DELETE in
    _get_or_create_customer_token: that only runs when somebody else logs in, so
    an abandoned token stayed valid indefinitely on a quiet week.
    """
    if not token:
        return None
    row = frappe.db.get_value("Customer Token", {"token": str(token).strip()},
                              ["mobile", "created"], as_dict=True)
    if not row:
        return None
    if row.created and frappe.utils.time_diff_in_seconds(
            now_datetime(), row.created) > CUSTOMER_TOKEN_DAYS * 86400:
        return None
    return row.mobile


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_submit_order_feedback():
    """Save one authenticated customer rating against one completed order."""
    data = frappe.local.form_dict
    token = str(data.get("token") or "").strip()
    mobile = _mobile_from_token(token)
    if not mobile:
        return {"success": False, "error": "Login required"}

    order_name = str(data.get("order_name") or "").strip()
    try:
        rating = int(data.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    comment = str(data.get("comment") or "").strip()[:1000]
    if not order_name or rating not in range(1, 6):
        return {"success": False,
                "error": "Order and a 1–5 star rating are required"}

    order = frappe.db.get_value(
        "POS Order",
        {"name": order_name, "mobile": ["in", phone_variants(mobile)]},
        ["name", "customer_name", "kitchen_status", "docstatus"],
        as_dict=True,
    )
    if not order:
        return {"success": False, "error": "Order not found"}
    if order.kitchen_status != "Served" and int(order.docstatus or 0) != 1:
        return {"success": False,
                "error": "Feedback is available after the order is complete"}

    existing = frappe.db.get_value("Comment", {
        "reference_doctype": "POS Order",
        "reference_name": order.name,
        "subject": CUSTOMER_FEEDBACK_SUBJECT,
    }, "name")
    if existing:
        return {"success": True, "already_submitted": True, "name": existing}

    customer_name = (order.customer_name
                     or frappe.db.get_value("Customer Account", {"mobile": mobile},
                                            "customer_name")
                     or "Customer")
    content = f"<p><strong>Customer rating: {rating}/5</strong></p>"
    if comment:
        content += f"<p>{html.escape(comment)}</p>"
    feedback = frappe.get_doc({
        "doctype": "Comment",
        "comment_type": "Comment",
        "reference_doctype": "POS Order",
        "reference_name": order.name,
        "subject": CUSTOMER_FEEDBACK_SUBJECT,
        "comment_by": customer_name,
        "comment_email": mobile,
        "content": content,
    })
    feedback.flags.ignore_permissions = True
    feedback.insert()
    return {"success": True, "name": feedback.name}
@frappe.whitelist(allow_guest=True)
def customer_get_profile():
    """Profile for the signed-in /app customer — drives the complete-your-profile
    prompt on app load (name/address/area; no OTP re-asks, token is the auth)."""
    token = (frappe.local.form_dict.get("token") or "").strip()
    mobile = _mobile_from_token(token)
    if not mobile:
        return {"success": False, "error": "Login required"}
    acc = frappe.db.get_value("Customer Account", {"mobile": mobile},
                              ["name", "customer_name"], as_dict=True)
    if not acc:
        return {"success": False, "error": "Account not found"}
    address = area = ""
    if frappe.db.has_column("Customer Account", "address"):
        address = frappe.db.get_value("Customer Account", acc.name, "address") or ""
        area = frappe.db.get_value("Customer Account", acc.name, "area") or ""
    return {"success": True, "name": acc.customer_name or "", "mobile": mobile,
            "address": address, "area": area,
            "profile_complete": bool((acc.customer_name or "") and address and area)}
@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_update_profile():
    """Save name/address/area for the token's account (no OTP — already signed in)."""
    data = frappe.local.form_dict
    mobile = _mobile_from_token((data.get("token") or "").strip())
    if not mobile:
        return {"success": False, "error": "Login required"}
    acc_name = frappe.db.get_value("Customer Account", {"mobile": mobile}, "name")
    if not acc_name:
        return {"success": False, "error": "Account not found"}
    if not frappe.db.has_column("Customer Account", "address"):
        _install_customer_profile_fields()
    updates = {}
    for field, src in (("customer_name", "name"), ("address", "address"), ("area", "area")):
        v = (data.get(src) or "").strip()
        if v:
            updates[field] = v
    if updates:
        frappe.db.set_value("Customer Account", acc_name, updates)
        frappe.db.commit()
    return {"success": True}
# ─── Phone number confirmation (OTP) for /cus registration ─────


def _send_customer_otp(mobile, code):
    """Send the customer verification code through Text.lk SMS only."""
    brand = settings.get("brand_name")
    if not sms.textlk_configured():
        return False, "sms"
    ok = sms.send_textlk_sms(
        _wa_number(mobile),
        f"{code} is your {brand} verification code. Valid for 5 minutes. "
        "Do not share this code.",
    )
    return ok, "sms"

@frappe.whitelist(allow_guest=True, methods=["POST"])
def sms_send_otp():
    """Send a 4-digit code so the customer confirms the phone is really theirs.

    Older installed PWAs can still use the legacy alias declared below, but
    delivery is SMS-only. Previous promo registration does not replace OTP
    verification when a customer requests a code.
    """
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    if not mobile or len(mobile) < 10:
        return {"sent": False, "error": "Valid mobile required"}
    cache = frappe.cache()
    if cache.get_value(f"sms_otp_cool:{mobile}", expires=True):
        channel = cache.get_value(f"sms_otp_channel:{mobile}", expires=True)
        channel = channel.decode() if isinstance(channel, bytes) else str(channel or "sms")
        return {"sent": True, "cooldown": True, "channel": channel}
    code = "".join(secrets.choice("0123456789") for _ in range(4))
    ok, channel = _send_customer_otp(mobile, code)
    if not ok:
        error = ("SMS service is not configured" if not sms.textlk_configured()
                 else "Could not send SMS — try again")
        return {"sent": False, "channel": channel, "error": error}
    cache.set_value(f"sms_otp:{mobile}", code, expires_in_sec=300)
    cache.set_value(f"sms_otp_channel:{mobile}", channel, expires_in_sec=300)
    cache.set_value(f"sms_otp_cool:{mobile}", 1, expires_in_sec=30)
    return {"sent": True, "channel": channel}
@frappe.whitelist(allow_guest=True, methods=["POST"])
def sms_verify_otp():
    """Check the SMS code; marks the mobile verified for 15 minutes.

    Every cache read in the OTP flow passes expires=True. Without it a miss is
    remembered in frappe.local.cache while set_value(expires_in_sec=...)
    deliberately does not update that dict — so the attempt counter never left
    zero and the cooldown never held whenever two of these calls shared a
    request. Separate HTTP requests hid it; a test does not.
    """
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    code = (frappe.local.form_dict.get("code") or "").strip()
    if not mobile or not code:
        return {"ok": False, "error": "Code required"}
    cache = frappe.cache()
    tries_key = f"sms_otp_tries:{mobile}"
    tries = int(cache.get_value(tries_key, expires=True) or 0)
    if tries >= 5:
        return {"ok": False, "error": "Too many attempts — request a new code"}
    real = cache.get_value(f"sms_otp:{mobile}", expires=True)
    if real is None:
        return {"ok": False, "error": "Code expired — tap Resend"}
    real = real.decode() if isinstance(real, bytes) else str(real)
    if code != real:
        cache.set_value(tries_key, tries + 1, expires_in_sec=300)
        return {"ok": False, "error": "Wrong code — check your messages"}
    cache.delete_value(f"sms_otp:{mobile}")
    cache.delete_value(f"sms_otp_channel:{mobile}")
    cache.delete_value(tries_key)
    cache.set_value(f"sms_verified:{mobile}", 1, expires_in_sec=900)
    return {"ok": True}


# Backward-compatible paths for older installed PWAs. Delivery is still SMS-only.
wa_send_otp = sms_send_otp
wa_verify_otp = sms_verify_otp
