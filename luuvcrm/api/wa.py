"""Sending a WhatsApp message, and getting a phone number into the shape the API wants.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _


def _send_wa_reply(mobile, message):
    """Send a text reply via WhatsApp. Returns True when the API accepted it."""
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return False
    try:
        import requests
        acc = frappe.get_doc("WhatsApp Account", wa_account)
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "text",
            "text": {"body": message},
        }
        resp = requests.post(url, headers=headers, json=data)
        return resp.status_code == 200
    except Exception as e:
        frappe.log_error(f"WA reply error: {str(e)}", "WhatsApp")
        return False
# The restaurant's WhatsApp business number — the one the sticker QRs open.
# Call wa_number() rather than reading the constant: the constant is the
# declared default for the first client, and settings.get() is the answer for
# whoever the site actually belongs to.
PROMO_WA_NUMBER = "94724444901"


def wa_number():
    from luuvcrm import settings
    return settings.get("wa_business_number")
def _wa_number(mobile):
    """Local 07XXXXXXXX → international 947XXXXXXXX for the WhatsApp Cloud API."""
    m = (mobile or "").strip().replace("+", "").replace(" ", "")
    if m.startswith("0") and len(m) == 10:
        return "94" + m[1:]
    return m
def _intl_number(mobile, dial="94"):
    """National/local number + a country dial code → international E.164 digits (no +) for the
    WhatsApp Cloud API. Strips non-digits and a single leading 0; if the number already begins
    with the dial code it's left as-is. dial defaults to Sri Lanka (94)."""
    m = "".join(ch for ch in (mobile or "") if ch.isdigit())
    d = "".join(ch for ch in (dial or "94") if ch.isdigit()) or "94"
    if not m:
        return ""
    if m.startswith("0"):
        m = m[1:]
    if m.startswith(d):
        return m
    return d + m
def _local_number(mobile):
    """International 947XXXXXXXX (webhook sender) → local 07XXXXXXXX (how Customer
    Account / POS Order mobiles are stored)."""
    m = (mobile or "").strip().replace("+", "").replace(" ", "")
    if m.startswith("94") and len(m) == 11:
        return "0" + m[2:]
    return m
def _normalize_wa_number(mobile, dial="94"):
    """Digits-only international form (94XXXXXXXXX): strip leading zeros and
    prepend the dial code if missing — same normalization the waiter page does
    client-side before building wa.me links."""
    digits = "".join(ch for ch in str(mobile or "") if ch.isdigit())
    digits = digits.lstrip("0")
    if digits and not digits.startswith(dial):
        digits = dial + digits
    if len(digits) < 10:
        frappe.throw(_("Enter a valid WhatsApp number"))
    return digits
