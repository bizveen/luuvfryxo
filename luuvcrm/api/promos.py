"""The sticker-QR free-meal campaign.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe

from luuvcrm import settings
from frappe import _
import secrets
from frappe.utils import now_datetime
from luuvcrm import roles

from luuvcrm.api.customers import (
    _get_or_create_customer_token,
    _install_customer_profile_fields,
)
from luuvcrm.api.wa import _wa_number
from luuvcrm.api import wa


# ─── QR Promo Campaign (sticker QR → register in /cus → free meal on first order) ─
# Flow: unique code per printed sticker → customer scans → wa.me WhatsApp msg →
# bot reply links to /cus (old /app?promo= / /customer?promo= links redirect to /cus?promo=) →
# registers → code Registered against their mobile → their first app order
# auto-adds the campaign's free item at Rs 0 → code Redeemed (single use).

PROMO_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L lookalikes
PROMO_WINDOW_HOURS = 6  # free-item orders must be for a time within this many hours (time optional → ASAP)
#: Default for the first client. Free-meal orders are pickup-only, collected
#: at this outlet and nowhere else; `settings.get("pickup_outlet")` is the value.
PROMO_PICKUP_OUTLET = "Kurunegala"
@frappe.whitelist()
def setup_scheduled_time_field():
    """Custom field: POS Order.scheduled_time — the customer's preferred time
    (free-item orders must be within PROMO_WINDOW_HOURS from now)."""
    frappe.only_for("System Manager")
    created = False
    if not frappe.db.has_column("POS Order", "scheduled_time"):
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        create_custom_field("POS Order", {
            "fieldname": "scheduled_time", "label": "Scheduled Time",
            "fieldtype": "Datetime", "insert_after": "kitchen_status"})
        created = True
    frappe.clear_cache()
    return {"scheduled_time_created": created}
def _install_qr_promo():
    """Create QR Promo Campaign + QR Promo Code DocTypes (idempotent; custom:1)."""
    created = []
    if not frappe.db.exists("DocType", "QR Promo Campaign"):
        frappe.get_doc({
            "doctype": "DocType", "name": "QR Promo Campaign", "module": "Zeloura", "custom": 1,
            "naming_rule": "By fieldname", "autoname": "field:campaign_name", "track_changes": 1,
            "fields": [
                {"fieldname": "campaign_name", "label": "Campaign Name", "fieldtype": "Data",
                 "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "free_item", "label": "Free Item", "fieldtype": "Link",
                 "options": "Item", "in_list_view": 1},
                {"fieldname": "active", "label": "Active", "fieldtype": "Check", "default": "1"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert(ignore_permissions=True)
        created.append("QR Promo Campaign")
    if not frappe.db.exists("DocType", "QR Promo Code"):
        frappe.get_doc({
            "doctype": "DocType", "name": "QR Promo Code", "module": "Zeloura", "custom": 1,
            "naming_rule": "By fieldname", "autoname": "field:code", "track_changes": 1,
            "fields": [
                {"fieldname": "code", "label": "Code", "fieldtype": "Data",
                 "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "campaign", "label": "Campaign", "fieldtype": "Link",
                 "options": "QR Promo Campaign", "in_list_view": 1},
                {"fieldname": "batch", "label": "Batch", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "status", "label": "Status", "fieldtype": "Select",
                 "options": "New\nRegistered\nRedeemed", "default": "New", "in_list_view": 1},
                {"fieldname": "mobile", "label": "Mobile", "fieldtype": "Data"},
                {"fieldname": "registered_on", "label": "Registered On", "fieldtype": "Datetime"},
                {"fieldname": "redeemed_on", "label": "Redeemed On", "fieldtype": "Datetime"},
                {"fieldname": "pos_order", "label": "POS Order", "fieldtype": "Link", "options": "POS Order"},
            ],
            "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
        }).insert(ignore_permissions=True)
        created.append("QR Promo Code")
    if not frappe.db.exists("QR Promo Campaign", "Free Meal Launch"):
        frappe.get_doc({"doctype": "QR Promo Campaign", "campaign_name": "Free Meal Launch",
                        "active": 1}).insert(ignore_permissions=True)
        created.append("Free Meal Launch campaign")
    frappe.db.commit()
    return {"status": "created" if created else "already_exists", "created": created}
@frappe.whitelist()
def setup_qr_promo():
    """Schema setup over HTTP. Admin only; the work is in _install_qr_promo."""
    frappe.only_for("System Manager")
    return _install_qr_promo()
def _active_promo_campaign():
    rows = frappe.get_all("QR Promo Campaign", filters={"active": 1},
                          fields=["name", "free_item"], limit=1)
    return rows[0] if rows else None
def _pending_promo_for(mobile):
    """Registered-but-unredeemed QR promo code for this mobile (or None)."""
    if not mobile or not frappe.db.exists("DocType", "QR Promo Code"):
        return None
    rows = frappe.get_all("QR Promo Code",
        filters={"mobile": mobile, "status": "Registered"},
        fields=["name", "campaign"], limit=1)
    if not rows:
        return None
    free_item = frappe.db.get_value("QR Promo Campaign", rows[0].campaign, "free_item")
    return {"name": rows[0].name, "campaign": rows[0].campaign, "free_item": free_item}
@frappe.whitelist(methods=["POST"])
def promo_set_item():
    """Manager sets/changes the free item on the active campaign."""
    roles.require_pos()
    item = (frappe.local.form_dict.get("item") or "").strip()
    if not item or not frappe.db.exists("Item", item):
        frappe.throw(_("Pick a valid menu item"))
    camp = _active_promo_campaign()
    if not camp:
        _install_qr_promo()
        camp = _active_promo_campaign()
    frappe.db.set_value("QR Promo Campaign", camp["name"], "free_item", item)
    frappe.db.commit()
    return {"ok": True, "campaign": camp["name"], "free_item": item,
            "item_name": frappe.db.get_value("Item", item, "item_name") or item}
@frappe.whitelist(methods=["POST"])
def create_promo_codes():
    """Bulk-generate N unique single-use codes for sticker printing; returns them all."""
    roles.require_pos()
    data = frappe.local.form_dict
    try:
        count = int(data.get("count") or 0)
    except Exception:
        count = 0
    count = max(1, min(count, 1000))
    batch = (data.get("batch") or "").strip() or str(frappe.utils.today())
    camp = _active_promo_campaign()
    if not camp:
        _install_qr_promo()
        camp = _active_promo_campaign()
    codes = []
    while len(codes) < count:
        code = "LF" + "".join(secrets.choice(PROMO_CODE_ALPHABET) for _ in range(6))
        if frappe.db.exists("QR Promo Code", code):
            continue
        frappe.get_doc({"doctype": "QR Promo Code", "code": code, "campaign": camp["name"],
                        "batch": batch, "status": "New"}).insert(ignore_permissions=True)
        codes.append(code)
    frappe.db.commit()
    return {"ok": True, "campaign": camp["name"], "batch": batch, "count": len(codes),
            "codes": codes}
@frappe.whitelist()
def get_promo_overview():
    """Campaign + batch stats + recent codes for the /qrpromo admin page."""
    roles.require_pos()
    if not frappe.db.exists("DocType", "QR Promo Code"):
        return {"setup": False, "campaign": None, "batches": [], "stats": {}}
    camp = _active_promo_campaign()
    item_name = ""
    if camp and camp.get("free_item"):
        item_name = frappe.db.get_value("Item", camp["free_item"], "item_name") or camp["free_item"]
    rows = frappe.db.sql(
        """
        SELECT batch,
               COUNT(*) AS total,
               SUM(status = 'New') AS new_cnt,
               SUM(status = 'Registered') AS registered_cnt,
               SUM(status = 'Redeemed') AS redeemed_cnt,
               MIN(creation) AS created
        FROM `tabQR Promo Code`
        GROUP BY batch ORDER BY MIN(creation) DESC
        """, as_dict=True)
    batches = [{"batch": r.batch or "", "total": int(r.total or 0), "new": int(r.new_cnt or 0),
                "registered": int(r.registered_cnt or 0), "redeemed": int(r.redeemed_cnt or 0),
                "created": str(r.created or "")[:10]} for r in rows]
    stats = {"total": sum(b["total"] for b in batches),
             "registered": sum(b["registered"] for b in batches),
             "redeemed": sum(b["redeemed"] for b in batches)}
    return {"setup": True,
            "campaign": {"name": camp["name"], "free_item": camp.get("free_item") or "",
                         "item_name": item_name} if camp else None,
            "batches": batches, "stats": stats}
@frappe.whitelist()
def get_promo_batch_codes(batch):
    """All codes of one batch (re-download / re-print a printed batch)."""
    roles.require_pos()
    rows = frappe.get_all("QR Promo Code", filters={"batch": batch},
                          fields=["code", "status"], order_by="creation asc")
    return {"batch": batch, "codes": [r.code for r in rows],
            "statuses": {r.code: r.status for r in rows}}
def _claim_promo_code(code, mobile):
    """Core claim: attach a New code to a mobile. No messaging side-effects —
    callers (web promo_register / WhatsApp webhook) send their own replies."""
    if not mobile or len(mobile) < 10 or not code:
        return {"ok": False, "error": "Valid mobile and code required"}
    if not frappe.db.exists("DocType", "QR Promo Code"):
        return {"ok": False, "error": "Promo not available"}
    row = frappe.db.get_value("QR Promo Code", {"code": code},
                              ["name", "status", "campaign", "mobile"], as_dict=True)
    if not row:
        return {"ok": False, "error": "Invalid promo code"}
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    if row.status != "New":
        if (row.mobile or "") == mobile:
            return {"ok": False, "already_mine": True, "item_name": item_name,
                    "error": "You already claimed this code"}
        return {"ok": False, "error": "This QR code was already used"}
    if frappe.db.exists("QR Promo Code",
                        {"mobile": mobile, "status": ["in", ["Registered", "Redeemed"]]}):
        return {"ok": False, "error": "This phone already claimed a free meal"}
    frappe.db.set_value("QR Promo Code", row.name,
                        {"status": "Registered", "mobile": mobile,
                         "registered_on": now_datetime()})
    frappe.db.commit()
    return {"ok": True, "item_name": item_name}
@frappe.whitelist(allow_guest=True)
def promo_register():
    """Attach a scanned QR promo code to a newly registered /app customer."""
    data = frappe.local.form_dict
    mobile = (data.get("mobile") or "").strip()
    code = (data.get("code") or "").strip().upper()
    res = _claim_promo_code(code, mobile)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "Could not claim"}
    item_name = res.get("item_name") or ""

    # WhatsApp confirmation to the customer (best-effort, never blocks the claim)
    wa_sent = False
    try:
        cust = frappe.db.get_value("Customer Account", {"mobile": mobile}, "customer_name") or ""
        what = item_name or "meal"
        msg = (
            f"🎁 *Luuv Fryxo — FREE {what}!*\n\n"
            f"Hi{(' ' + cust) if cust else ''}! Your free {what} is locked in ✅\n\n"
            f"Order anything on our app and your free {what} is added to your FIRST order automatically.\n\n"
            f"👉 Order now: {settings.url('/cus')}\n\n"
            f"Offer: one per customer, first app order only."
        )
        wa_sent = bool(wa._send_wa_reply(_wa_number(mobile), msg))
    except Exception as e:
        frappe.log_error(f"Promo WhatsApp error: {str(e)}", "QR Promo")
    return {"ok": True, "item_name": item_name, "whatsapp_sent": wa_sent}
@frappe.whitelist(allow_guest=True)
def promo_check():
    """Validate a scanned promo code for the /app landing banner (no side effects)."""
    code = (frappe.local.form_dict.get("code") or "").strip().upper()
    if not code or not frappe.db.exists("DocType", "QR Promo Code"):
        return {"valid": False, "used": False}
    row = frappe.db.get_value("QR Promo Code", {"code": code}, ["status", "campaign"], as_dict=True)
    if not row:
        return {"valid": False, "used": False}
    if row.status != "New":
        return {"valid": False, "used": True}
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    return {"valid": True, "used": False, "item_name": item_name or "meal"}
def _valid_claim_pair(phone, code):
    """The (phone, code) pair is legit only if that code was claimed BY that phone
    via the WhatsApp webhook — so a /claim link can't be forged for another number."""
    if not phone or not code or not frappe.db.exists("DocType", "QR Promo Code"):
        return None
    row = frappe.db.get_value("QR Promo Code", {"code": code},
                              ["name", "status", "campaign", "mobile"], as_dict=True)
    if not row or (row.mobile or "") != phone or row.status not in ("Registered", "Redeemed"):
        return None
    free_item = frappe.db.get_value("QR Promo Campaign", row.campaign, "free_item")
    item_name = (frappe.db.get_value("Item", free_item, "item_name") or free_item) if free_item else ""
    return {"item_name": item_name}
@frappe.whitelist(allow_guest=True, methods=["POST"])
def claim_save_details():
    """Save the customer's details from the /claim form (sent via WhatsApp reply).
    The number is already proven — the claim came from their own WhatsApp."""
    data = frappe.local.form_dict
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "").strip()
    pin = (data.get("pin") or "").strip()

    address = (data.get("address") or "").strip()
    area = (data.get("area") or "").strip()

    pair = _valid_claim_pair(phone, code)
    if not pair:
        return {"ok": False, "error": "This claim link is not valid"}
    item_name = pair["item_name"]

    existing = frappe.db.get_value("Customer Account", {"mobile": phone}, "name")
    if existing:
        return {"ok": True, "exists": True, "item_name": item_name,
                "name": frappe.db.get_value("Customer Account", existing, "customer_name") or ""}

    if not pin or len(pin) != 4 or not pin.isdigit():
        return {"ok": False, "error": "PIN must be 4 digits"}

    if not frappe.db.has_column("Customer Account", "address"):
        _install_customer_profile_fields()
    account = frappe.get_doc({
        "doctype": "Customer Account",
        "mobile": phone,
        "customer_name": name,
        "pin": pin,
        "address": address,
        "area": area,
        "last_login": now_datetime(),
    })
    account.flags.ignore_permissions = True
    account.insert(ignore_permissions=True)
    frappe.db.commit()
    token = _get_or_create_customer_token(phone)

    # WhatsApp confirmation — details saved, offer locked (best-effort)
    try:
        what = item_name or "meal"
        wa._send_wa_reply(_wa_number(phone),
            f"✅ Details saved{(', ' + name) if name else ''}!\n\n"
            f"🎁 Your FREE {what} is locked in — it's added to your FIRST order "
            f"automatically.\n⏰ Free-meal orders are for within {PROMO_WINDOW_HOURS} hours."
            f"\n📍 Pickup only — collect at our {settings.get('pickup_outlet')} outlet (no delivery).\n\n"
            f"👉 Order now: {settings.url('/cus')}")
    except Exception as e:
        frappe.log_error("QR Promo claim WA", str(e))

    return {"ok": True, "token": token, "name": name, "item_name": item_name}
@frappe.whitelist(allow_guest=True)
def promo_status():
    """Pending free-meal info for a mobile — powers the banner/popup in /cus.
    Includes the within-N-hours window so the client caps the time picker."""
    from frappe.utils import add_to_date
    mobile = (frappe.local.form_dict.get("mobile") or "").strip()
    promo = _pending_promo_for(mobile)
    if not promo or not promo.get("free_item"):
        return {"pending": bool(promo), "item_name": ""}
    item_name = frappe.db.get_value("Item", promo["free_item"], "item_name") or promo["free_item"]
    max_time = add_to_date(now_datetime(), hours=PROMO_WINDOW_HOURS).strftime("%Y-%m-%d %H:%M:%S")
    return {"pending": True, "item_name": item_name,
            "window_hours": PROMO_WINDOW_HOURS, "max_time": max_time,
            "pickup_outlet": settings.get("pickup_outlet")}
