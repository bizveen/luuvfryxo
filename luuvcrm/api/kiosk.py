"""The self-order kiosk at the table.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import hmac

import frappe

from luuvcrm import branch, settings
from frappe import _
from frappe.utils import now_datetime

from luuvcrm.api.loyalty import (
    _apply_referral_for_mobile,
    _claim_first_order_referral_discount,
    _redeem_cashback,
    _new_reward_code,
)
from luuvcrm.api.offers import prepare_customer_offer, record_customer_offer
from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _is_takeaway,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.promos import PROMO_WINDOW_HOURS, _pending_promo_for
from luuvcrm.api.push import guest_order_push_ticket
from luuvcrm.api.schema import _clean_item_note, _ensure_item_notes_field
from luuvcrm.api.wa import wa_number
from luuvcrm.api.whatsapp import send_customer_whatsapp, send_order_whatsapp


# ─── Kiosk Self-Ordering ───────────────────────────────

@frappe.whitelist(allow_guest=True)
def kiosk_place_order():
    data = frappe.local.form_dict
    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Online Guest"
    mobile = data.get("mobile", "").strip() or ""
    ref_code = data.get("ref_code", "").strip().upper()
    selected_offer = str(data.get("offer_name") or "").strip()
    table = data.get("table", "")
    order_type = data.get("order_type", "")
    is_cus = str(data.get("order_channel") or "").strip().upper() == "CUS"
    # A signed-in customer never has to retype (or trust the browser to post)
    # their phone number. Resolve it from the same session token used by the
    # orders/profile APIs. Logged-out checkout can still post a phone number;
    # the customer app verifies an unknown number before it reaches this call.
    customer_token = str(data.get("customer_token") or "").strip()
    if is_cus and customer_token:
        from luuvcrm.api.customers import _mobile_from_token

        signed_in_mobile = _mobile_from_token(customer_token)
        if not signed_in_mobile:
            return {"error": "Your session expired — sign in again"}
        mobile = signed_in_mobile
    if is_cus and order_type not in ("Dine In", "Take Away", "Delivery"):
        order_type = "Take Away"
    # Customer-app orders wait for a staff call and KOT; WhatsApp no longer
    # confirms them. Keep the older kiosk/QR confirmation path unchanged.
    wa_confirm = not is_cus and str(data.get("wa_confirm") or "") in ("1", "true", "True")
    held = is_cus or wa_confirm
    scheduled_raw = (data.get("scheduled_time") or "").strip()
    restaurant = branch.public(restaurant=(data.get("restaurant") or "").strip())

    # Validate table exists — if not, silently ignore it
    if table:
        table_exists = frappe.db.get_value("Restaurant Table", table, "name")
        if not table_exists:
            table = ""

    # ── preferred time + free-item window rule (free meal must be within N hours) ──
    from frappe.utils import get_datetime, add_to_date
    scheduled_dt = None
    if scheduled_raw:
        try:
            scheduled_dt = get_datetime(scheduled_raw)
        except Exception:
            scheduled_dt = None
    # A past preferred time is meaningless → treat as ASAP (drop it).
    if scheduled_dt and scheduled_dt < now_datetime():
        scheduled_dt = None
    promo = _pending_promo_for(mobile)
    if promo and promo.get("free_item"):
        # Free meal = pickup-only at the restaurant selected in the customer app
        # → force Take Away (no delivery, no dine-in table, no service charge).
        order_type = "Take Away"
        table = ""
        # Preferred time must be within the next PROMO_WINDOW_HOURS. Time is optional
        # (no time = ASAP, within the window); only a time TOO FAR ahead is rejected.
        max_dt = add_to_date(now_datetime(), hours=PROMO_WINDOW_HOURS)
        if scheduled_dt and scheduled_dt > max_dt:
            return {"error": f"Free-meal orders must be within the next {PROMO_WINDOW_HOURS} hours — "
                             f"pick a time before {max_dt.strftime('%d %b %I:%M %p')}.",
                    "max_time": max_dt.strftime("%Y-%m-%d %H:%M:%S")}

    order_source = "Online"
    waiter_name = "CUS" if is_cus else "Kiosk"

    # Attach the referral before the order is inserted.  The order endpoint is
    # the authority for this path; the client cannot grant itself a discount by
    # merely posting an amount or percentage.
    referral_link = {"success": False}
    if mobile and ref_code:
        referral_link = _apply_referral_for_mobile(mobile, ref_code)

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        branch.FIELD: restaurant,
        "restaurant_table": table,
        "order_source": order_source,
        **({"order_type": order_type} if is_cus else {}),
        # Draft stays out of the kitchen until the relevant confirmation.
        "kitchen_status": "Draft" if held else "Pending",
        "grand_total": 0,
        "service_charge_rate": 0,
        "service_charge_amount": 0,
        "items": [],
    })
    subtotal = 0
    sc_base = 0
    priced_items = []
    if any(_clean_item_note(item.get("notes")) for item in items):
        _ensure_item_notes_field()
    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = _server_item_rate(item_code)
        item_ta = _item_is_takeaway(item_data, order_type)
        item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
        item_row = {
            "item": item_code,
            "item_name": item_name,
            "qty": qty,
            "rate": rate,
            "takeaway": 1 if item_ta else 0,
        }
        item_note = _clean_item_note(item_data.get("notes"))
        if item_note:
            item_row["notes"] = item_note
        pos_order.append("items", item_row)
        priced_items.append({"item": item_code, "qty": qty, "rate": rate})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    if selected_offer and promo and promo.get("free_item"):
        return {"error": "The free-meal promotion cannot be combined with another offer"}
    offer_result = {"applied": False}
    if selected_offer and is_cus:
        offer_result = prepare_customer_offer(
            selected_offer, mobile, restaurant, priced_items, subtotal
        )
        if not offer_result.get("applied"):
            return {"error": offer_result.get("error") or "Offer could not be applied"}
        if offer_result.get("free_item") and offer_result.get("free_quantity"):
            free_code = offer_result["free_item"]
            free_name = frappe.db.get_value("Item", free_code, "item_name") or free_code
            pos_order.append("items", {
                "item": free_code,
                "item_name": free_name,
                "qty": offer_result["free_quantity"],
                "rate": 0,
                "takeaway": 1 if _is_takeaway(order_type) else 0,
                "notes": f"FREE · {offer_result.get('title') or selected_offer}",
            })

    # QR promo: registered sticker code + campaign free item → add at Rs 0 (first order)
    promo_item_name = ""
    if promo and promo.get("free_item"):
        fi = promo["free_item"]
        promo_item_name = frappe.db.get_value("Item", fi, "item_name") or fi
        pos_order.append("items", {"item": fi, "item_name": promo_item_name, "qty": 1,
                                   "rate": 0, "takeaway": 1 if _is_takeaway(order_type) else 0})

    sc_rate, sc_amount = _service_charge_on(sc_base)
    pos_order.grand_total = subtotal + sc_amount
    pos_order.service_charge_rate = sc_rate
    pos_order.service_charge_amount = sc_amount
    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()

    referral_discount = {"applied": False, "amount": 0, "percent": 0}
    if referral_link.get("success") and (
            not offer_result.get("applied") or offer_result.get("stackable")):
        referral_discount = _claim_first_order_referral_discount(
            mobile, ref_code, pos_order.name, subtotal
        )
    discounts = []
    total_discount = 0.0
    if referral_discount.get("applied"):
        total_discount += float(referral_discount["amount"] or 0)
        discounts.append(f"Referral {referral_discount['percent']:g}% · {ref_code}")
    if offer_result.get("applied"):
        total_discount += float(offer_result.get("order_discount") or 0)
        discounts.append(offer_result.get("title") or selected_offer)
        pos_order.db_set("customer_offer", offer_result["offer"])
        pos_order.db_set("offer_discount_amount", offer_result.get("benefit_amount") or 0)
    if total_discount:
        pos_order.db_set("discount_amount", total_discount)
    if discounts:
        pos_order.db_set("discount_note", " · ".join(discounts)[:140])
    _apply_takeaway_service_charge(pos_order)
    reward_redeemed = 0.0
    if is_cus and str(data.get("use_rewards") or "").lower() in ("1", "true", "yes"):
        if not customer_token:
            frappe.throw(_("Sign in to use rewards"))
        loyalty_name = frappe.db.get_value("Loyalty Customer", {"mobile": mobile}, "name")
        available = float(frappe.db.get_value("Loyalty Customer", loyalty_name, "cashback_balance") or 0) if loyalty_name else 0
        # Rewards use the same pre-service-charge discount base as POS payment.
        # Capping at grand_total could debit service charge too and create an
        # invoice discount larger than its item subtotal.
        remaining_subtotal = max(0.0, round(subtotal - float(pos_order.get("discount_amount") or 0), 2))
        reward_redeemed = round(min(available, remaining_subtotal), 2)
        if reward_redeemed <= 0:
            frappe.throw(_("No rewards are available for this order"))
        result = _redeem_cashback(mobile, reward_redeemed,
                                  "Applied to customer app order", pos_order.name,
                                  respect_minimum=False)
        if not result.get("success"):
            frappe.throw(_(result.get("error") or "Could not apply rewards"))
        request = frappe.get_doc({
            "doctype": "Reward Redemption Request", "code": _new_reward_code(),
            "loyalty_customer": loyalty_name, "mobile": mobile,
            "amount": reward_redeemed, "status": "Approved",
            "expires_on": add_to_date(now_datetime(), minutes=10),
            "pos_order": pos_order.name, "redeemed_by": "Customer app",
        }).insert(ignore_permissions=True)
        pos_order.db_set("reward_redeem_amount", reward_redeemed)
        pos_order.db_set("reward_redemption_request", request.name)
        _apply_takeaway_service_charge(pos_order)
    offer_redemption = record_customer_offer(offer_result, pos_order)
    if scheduled_dt and frappe.db.has_column("POS Order", "scheduled_time"):
        pos_order.db_set("scheduled_time", scheduled_dt)
    if wa_confirm:
        pos_order.db_set("whatsapp_status", "Awaiting Confirmation")
    if not held:
        from luuvcrm.api.inventory import consume_order_stock

        consume_order_stock(pos_order.name)
    frappe.db.commit()

    # single use: mark the promo code redeemed against this order
    if promo and promo.get("free_item"):
        try:
            frappe.db.set_value("QR Promo Code", promo["name"],
                                {"status": "Redeemed", "redeemed_on": now_datetime(),
                                 "pos_order": pos_order.name})
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Promo redeem error: {str(e)}", "QR Promo")

    if not held:
        # Referral cashback is awarded by payment, not order creation.
        try:
            send_order_whatsapp(pos_order.name)
        except Exception as e:
            frappe.log_error(f"Restaurant WhatsApp error: {str(e)}", "WhatsApp")
        if mobile:
            try:
                send_customer_whatsapp(pos_order.name, mobile)
            except Exception as e:
                frappe.log_error(f"Customer WhatsApp error: {str(e)}", "WhatsApp")

    confirm_link = ""
    if wa_confirm:
        from urllib.parse import quote
        confirm_link = ("https://wa.me/" + wa_number() + "?text="
                        + quote(f"Confirming my order #{pos_order.name} ✅"))

    status_ticket = ""
    if not is_cus:
        from luuvcrm.api.orders import track_signature

        status_ticket = track_signature(pos_order.name)

    return {
        "name": pos_order.name,
        "grand_total": pos_order.grand_total,
        "subtotal": subtotal,
        "service_charge_rate": pos_order.service_charge_rate or 0,
        "service_charge_amount": pos_order.service_charge_amount or 0,
        "referral_code": ref_code if referral_link.get("success") else "",
        "referral_discount_applied": bool(referral_discount.get("applied")),
        "referral_discount_percent": referral_discount.get("percent") or 0,
        "referral_discount_amount": referral_discount.get("amount") or 0,
        "offer_applied": bool(offer_result.get("applied")),
        "offer_name": offer_result.get("offer") or "",
        "offer_title": offer_result.get("title") or "",
        "offer_saving": offer_result.get("benefit_amount") or 0,
        "reward_redeemed": reward_redeemed,
        "offer_redemption": offer_redemption or "",
        "table": table,
        "customer_name": customer_name,
        "mobile": mobile,
        "promo_item": promo_item_name,
        "pickup_outlet": (restaurant or settings.get("pickup_outlet")) if promo_item_name else "",
        "scheduled_time": scheduled_dt.strftime("%Y-%m-%d %H:%M:%S") if scheduled_dt else "",
        "needs_wa_confirm": wa_confirm,
        "needs_call_confirm": is_cus,
        "order_channel": waiter_name,
        "guest_push_ticket": guest_order_push_ticket(pos_order.name) if is_cus else "",
        "status_ticket": status_ticket,
        "confirm_link": confirm_link,
    }


@frappe.whitelist(allow_guest=True)
def kiosk_order_status(order_name=None, status_ticket=None):
    """Return the kitchen state for one signed kiosk order."""
    from luuvcrm.api.orders import track_signature

    order_name = str(order_name or "").strip()
    supplied = str(status_ticket or "").strip()
    expected = track_signature(order_name) if order_name else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        return {"success": False}
    order = frappe.db.get_value(
        "POS Order", order_name,
        ["name", "waiter_name", "order_source", "kitchen_status"],
        as_dict=True,
    )
    if (not order or order.order_source != "Online"
            or str(order.waiter_name or "").strip().upper() != "KIOSK"):
        return {"success": False}
    return {
        "success": True,
        "order_name": order.name,
        "status": order.kitchen_status or "Pending",
    }
# ─── Kiosk Setup ─────────────────────────────────────────

@frappe.whitelist()
def setup_kiosk_tools():
    """One-time setup: create Item Review DocType + custom fields on Item."""
    frappe.only_for("System Manager")
    if frappe.db.exists("DocType", "Item Review"):
        return {"status": "already_exists"}

    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Item Review",
        "module": "Zeloura",
        "custom": 1,
        "fields": [
            {"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1, "in_list_view": 1},
            {"fieldname": "customer_name", "label": "Customer Name", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
            {"fieldname": "rating", "label": "Rating", "fieldtype": "Rating", "reqd": 1, "in_list_view": 1},
            {"fieldname": "comment", "label": "Comment", "fieldtype": "Small Text"},
            {"fieldname": "submitted_by", "label": "Submitted By", "fieldtype": "Data"},
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
            {"role": "Guest", "read": 1, "write": 1, "create": 1},
        ],
    })
    doc.insert()

    fields = [
        {"dt": "Item", "fieldname": "speciality_tags", "label": "Speciality Tags", "fieldtype": "Small Text", "description": "Comma-separated: Chef's Special, Most Popular, Spicy, Vegan, Gluten-Free"},
        {"dt": "Item", "fieldname": "youtube_url", "label": "YouTube Video URL", "fieldtype": "Data"},
        {"dt": "Item", "fieldname": "prep_time", "label": "Preparation Time (mins)", "fieldtype": "Int"},
    ]
    for f in fields:
        if not frappe.db.exists("Custom Field", {"dt": f["dt"], "fieldname": f["fieldname"]}):
            cf = frappe.get_doc({
                "doctype": "Custom Field",
                "dt": f["dt"],
                "fieldname": f["fieldname"],
                "label": f["label"],
                "fieldtype": f["fieldtype"],
                "description": f.get("description", ""),
                "insert_after": "image",
            })
            cf.insert()

    return {"status": "created"}
