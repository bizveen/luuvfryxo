"""Outbound order messages and the inbound reply router.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe

from luuvcrm import settings
from frappe.utils import now_datetime

from luuvcrm.api.guides import (
    guide_app_url,
    GUIDE_CODE_RE,
    _gen_guide_password,
    _get_or_create_guide_token,
    _guide_default_percent,
    _hash_guide_pw,
)
from luuvcrm.api.loyalty import _process_cashback
from luuvcrm.api.promos import PROMO_PICKUP_OUTLET, PROMO_WINDOW_HOURS, _claim_promo_code
from luuvcrm.api.wa import _intl_number, _local_number, _normalize_wa_number
from luuvcrm.api import wa


# ─── WhatsApp Order Confirmation ─────────────────────────

def send_order_whatsapp(order_name):
    """Send order details to the restaurant's WhatsApp business number."""
    order = frappe.get_doc("POS Order", order_name)

    if not frappe.db.exists("WhatsApp Account", {}):
        return

    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return

    acc = frappe.get_doc("WhatsApp Account", wa_account)

    items_text = ""
    for i in (order.get("items") or []):
        items_text += f"- {i.item_name or i.item} x{i.qty} = LKR {i.rate * i.qty:.0f}\n"

    sched_line = ""
    if frappe.db.has_column("POS Order", "scheduled_time") and order.get("scheduled_time"):
        sched_line = ("⏰ *PRE-ORDER for "
                      + frappe.utils.format_datetime(order.scheduled_time, "d MMM h:mm a")
                      + "*\n")

    message = (
        f"*New Order - {order.name}*\n"
        f"{sched_line}"
        f"👤 {order.customer_name or 'Online Guest'}\n"
        f"🪑 {order.restaurant_table or 'N/A'}\n"
        f"📱 {order.mobile or 'N/A'}\n"
        f"📝 {order.notes or ''}\n\n"
        f"*Items:*\n{items_text}\n"
        f"*Total:* LKR {order.grand_total:.0f}\n\n"
        f"Reply *OK* to accept this order."
    )

    # The number the restaurant watches for new orders. A setting, not a
    # literal — this is the first thing a second restaurant changes.
    to_number = settings.get("wa_order_number")

    try:
        import requests
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": message, "preview_url": False},
        }
        resp = requests.post(url, headers=headers, json=data)
        result = resp.json()
        if resp.status_code == 200 and "messages" in result:
            msg_id = result["messages"][0]["id"]
            frappe.db.set_value("POS Order", order.name, "wa_message_id", msg_id)
            frappe.db.set_value("POS Order", order.name, "whatsapp_status", "Sent")
            frappe.db.commit()
            frappe.log_error(f"Order notification sent for {order.name}", "WhatsApp")
        else:
            frappe.log_error(f"Order notification failed for {order.name}: {resp.text}", "WhatsApp")
    except Exception as e:
        frappe.log_error(f"WhatsApp error for {order.name}: {str(e)}", "WhatsApp")
def send_customer_whatsapp(order_name, mobile, dial="94"):
    """Send tracking link to customer via WhatsApp. `dial` = the customer's country dial code
    (default Sri Lanka 94); the number is normalised to international E.164 for the Cloud API."""
    to = _intl_number(mobile, dial)
    if not to or not frappe.db.exists("WhatsApp Account", {}):
        return
    wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
    if not wa_account:
        return

    track_url = settings.url(f"/cus?phone={mobile}")
    message = (
        f"*Order #{order_name} - {settings.get('brand_name')}* 🎉\n\n"
        f"Thank you for your order!\n\n"
        f"View your orders: {track_url}\n\n"
        f"Your order is pending confirmation. Please wait for the restaurant to accept it."
    )

    try:
        import requests
        acc = frappe.get_doc("WhatsApp Account", wa_account)
        token = acc.get_password("token")
        url = f"{acc.url}/{acc.version}/{acc.phone_id}/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message, "preview_url": True}}
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code == 200:
            frappe.log_error(f"Tracking link sent to {to} for {order_name}", "WhatsApp")
    except Exception as e:
        frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")
def process_wa_reply(doc, method):
    """Auto-reply to incoming WhatsApp messages."""
    if doc.type != "Incoming":
        return

    mobile = doc.from_field if hasattr(doc, "from_field") else doc.get("from")
    msg_text = (doc.message or "").strip().upper()

    # Sticker QR → wa.me prefilled message ("… Code: LFXXXXXX") → claim the promo
    # against the SENDER's number (self-verifying) and auto-reply the app link.
    import re
    pm = re.search(r"\bLF[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}\b", msg_text)
    if pm and frappe.db.exists("DocType", "QR Promo Code") \
            and frappe.db.exists("QR Promo Code", pm.group(0)):
        code = pm.group(0)
        local = _local_number(mobile)
        res = _claim_promo_code(code, local)
        if res.get("ok") or res.get("already_mine"):
            item = res.get("item_name") or "meal"
            lead_note = (f"\n⏰ Free-meal orders are for within {PROMO_WINDOW_HOURS} hours."
                         f"\n📍 Pickup only — collect at our {settings.get('pickup_outlet')} outlet (no delivery).")
            if frappe.db.exists("Customer Account", {"mobile": local}):
                # already registered — nothing to fill, straight to ordering
                wa._send_wa_reply(mobile,
                    f"🎁 Your FREE {item} is unlocked!\n\n"
                    f"You're all set — order here:\n{settings.url(f'/cus?phone={local}')}\n\n"
                    f"Your free {item} is added to your FIRST order automatically."
                    f"{lead_note}\n\nSee you soon! 🍔")
            else:
                # send the registration details form (number is proven by this message)
                wa._send_wa_reply(mobile,
                    f"🎁 Your FREE {item} is reserved!\n\n"
                    f"Save your details here to lock it in:\n"
                    f"{settings.url(f'/claim?phone={local}&code={code}')}\n\n"
                    f"Takes 20 seconds — then your free {item} is added to your "
                    f"first order automatically.{lead_note} 🍔")
        else:
            wa._send_wa_reply(mobile,
                f"⚠️ {res.get('error') or 'Could not claim this code'}\n\n"
                f"You can still order anytime: {settings.url('/cus')}")
        try:
            frappe.log_error("QR Promo claim", f"{code} by {mobile} -> {res}")
        except Exception:
            pass
        return

    # Guide partner self-registration: guide sends "GUIDE" (wa.me deep link on the
    # /guide login screen) → create/refresh their Tour Guide record and auto-reply
    # the app link + a fresh password. The sender's number is proven by the message
    # itself, and the reply rides the 24-hour service window the guide just opened
    # (no Meta template needed). Wrapped so a failure can never break the webhook.
    # Guide partner registration/login over WhatsApp. NEW registrations need a
    # staff-issued invite code (GDXXXXXX) — a bare "guide" in an ordinary
    # customer message must never create a Tour Guide record. Numbers that are
    # already registered just get their magic login link back (self-serve).
    gcode = re.search(GUIDE_CODE_RE, msg_text)
    if (gcode or re.search(r"\bGUIDE\b", msg_text)) and frappe.db.exists("DocType", "Tour Guide"):
        try:
            number = _normalize_wa_number(mobile)
            gname = frappe.db.get_value("Tour Guide", {"whatsapp": number}, "name")
            if gname:
                # Known partner: fresh magic link — the token IS the session,
                # WhatsApp possession is the auth root.
                token = _get_or_create_guide_token(gname)
                pct = float(frappe.db.get_value("Tour Guide", gname, "cashback_percent")
                            or _guide_default_percent())
                wa._send_wa_reply(mobile,
                    f"🧭 Welcome back to the {settings.get('brand_name')} partner program!\n\n"
                    f"Tap your personal link to see your cashback:\n"
                    f"{guide_app_url()}?t={token}\n\n"
                    f"You earn {pct:g}% cashback on every bill you bring us. 🍔")
            elif gcode and frappe.db.exists("DocType", "Guide Invite Code"):
                row = frappe.db.get_value("Guide Invite Code",
                    {"code": gcode.group(0)}, ["name", "status"], as_dict=True)
                if row and row.status == "New":
                    default_pct = _guide_default_percent()
                    guide = frappe.get_doc({
                        "doctype": "Tour Guide",
                        "whatsapp": number,
                        "role": "Guide",
                        "cashback_percent": default_pct,
                        "status": "Invited",
                        # random unused hash — login is via the magic link
                        "password_hash": _hash_guide_pw(_gen_guide_password()),
                    })
                    guide.insert(ignore_permissions=True)
                    frappe.db.set_value("Guide Invite Code", row.name,
                        {"status": "Used", "used_by": guide.name, "used_on": now_datetime()})
                    token = _get_or_create_guide_token(guide.name)
                    wa._send_wa_reply(mobile,
                        f"🧭 Welcome to the {settings.get('brand_name')} partner program!\n\n"
                        f"You earn {default_pct:g}% cashback on every "
                        f"customer bill you bring us.\n\n"
                        f"Tap your personal link to finish registering — just add "
                        f"your name and details:\n{guide_app_url()}?t={token}\n\n"
                        "See you soon! 🍔")
                else:
                    wa._send_wa_reply(mobile,
                        "⚠️ That partner code has already been used or isn't valid.\n\n"
                        "Please ask our staff for a new partner code. 🙏")
            else:
                wa._send_wa_reply(mobile,
                    f"🧭 {settings.get('brand_name')} partner program — earn cashback on every "
                    "customer bill you bring us!\n\n"
                    "To join, ask our staff for your personal partner code, then "
                    "send it here (it looks like GD3XK7MP).")
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Guide WA register")
        return

    # App order confirmation: "Confirming my order #POS-XXXXX" from the customer.
    # Only the number that placed the order can confirm it; then it enters the kitchen.
    om = re.search(r"POS-\d+", msg_text)
    if om and frappe.db.exists("POS Order", om.group(0)):
        oname = om.group(0)
        local = _local_number(mobile)
        row = frappe.db.get_value("POS Order", oname,
                                  ["whatsapp_status", "mobile", "grand_total"], as_dict=True)
        if row and row.whatsapp_status == "Awaiting Confirmation":
            if (row.mobile or "") != local:
                wa._send_wa_reply(mobile,
                    "⚠️ We couldn't match this order to your number. Please confirm "
                    "from the phone that placed the order.")
                return
            frappe.db.set_value("POS Order", oname,
                                {"whatsapp_status": "Confirmed", "kitchen_status": "Pending"})
            frappe.db.commit()
            sched_txt = ""
            if frappe.db.has_column("POS Order", "scheduled_time"):
                st = frappe.db.get_value("POS Order", oname, "scheduled_time")
                if st:
                    sched_txt = ("\n⏰ Scheduled for "
                                 + frappe.utils.format_datetime(st, "d MMM h:mm a"))
            try:
                if row.grand_total:
                    _process_cashback(local, oname, float(row.grand_total or 0))
            except Exception:
                pass
            try:
                send_order_whatsapp(oname)
            except Exception as e:
                frappe.log_error("Order WA notify", str(e))
            wa._send_wa_reply(mobile,
                f"✅ Order {oname} confirmed!{sched_txt}\n\n"
                f"Track it: {settings.url(f'/cus?phone={local}')}\n\nThank you! 🍔")
            return
        if row and row.whatsapp_status in ("Confirmed", "Sent"):
            wa._send_wa_reply(mobile,
                f"👍 Order {oname} is already confirmed.\n"
                f"Track it: {settings.url(f'/cus?phone={_local_number(mobile)}')}")
            return

    if msg_text in ("OK", "ACCEPT"):
        order_name = frappe.db.get_value(
            "POS Order",
            {"whatsapp_status": "Sent", "docstatus": 0},
            order_by="creation desc",
        )
        if order_name:
            frappe.db.set_value("POS Order", order_name, "whatsapp_status", "Confirmed")
            frappe.db.set_value("POS Order", order_name, "kitchen_status", "Processing")
            wa._send_wa_reply(mobile, "✅ Order accepted! We are preparing your food.")
            frappe.log_error(f"Order {order_name} accepted via WhatsApp", "WhatsApp")
            return

    auto_reply = (
        f"Thank you for messaging {settings.get('brand_name')}! 🎉\n\n"
        "View your orders: " + settings.url("/cus") + "\n"
        "To confirm a pending order, reply OK\n\n"
        "We will get back to you shortly!"
    )
    wa._send_wa_reply(mobile, auto_reply)
    frappe.log_error(f"Auto-reply sent to {mobile}: {msg_text}", "WhatsApp")
