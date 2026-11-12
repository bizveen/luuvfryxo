"""Manager-authored customer campaigns and their server-side redemption.

The browser chooses an offer; this module independently checks its schedule,
branch, eligible items, minimum spend and limits, then calculates the benefit
from authoritative item prices.  Analytics are derived from deduplicated view /
claim events and actual POS-order redemptions.
"""

import hashlib
import frappe
from frappe import _
from frappe.utils import get_time, now_datetime, today

from luuvcrm import branch, roles


OFFER_TYPES = {
    "Buy X Get Y Free",
    "Percentage Off",
    "Free Item",
    "Announcement",
}


def _clean_int(value, default=0, minimum=0, maximum=999):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _clean_float(value, default=0, minimum=0, maximum=100):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _csv_values(value):
    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def _daily_window(row):
    """Return an intentional daily window.

    Frappe fills omitted Time fields with the insertion time on some versions.
    Those two generated values are milliseconds apart; treating that as a real
    schedule would make a newly-created offer disappear immediately.
    """
    start = get_time(row.daily_start_time) if row.get("daily_start_time") else None
    end = get_time(row.daily_end_time) if row.get("daily_end_time") else None
    if start and end:
        a = start.hour * 3600 + start.minute * 60 + start.second
        b = end.hour * 3600 + end.minute * 60 + end.second
        if abs(a - b) < 60:
            return None, None
    return start, end


def _offer_payload(row, stats=None):
    daily_start, daily_end = _daily_window(row)
    payload = {
        "name": row.name,
        "offer_name": row.offer_name,
        "title": row.title,
        "description": row.description or "",
        "offer_type": row.offer_type,
        "buy_quantity": int(row.buy_quantity or 0),
        "free_quantity": int(row.free_quantity or 0),
        "discount_percent": float(row.discount_percent or 0),
        "free_item": row.free_item or "",
        "free_item_name": (
            frappe.db.get_value("Item", row.free_item, "item_name") or row.free_item
            if row.free_item else ""
        ),
        "eligible_item_group": row.get("eligible_item_group") or "",
        "eligible_items": row.get("eligible_items") or "",
        "minimum_order_amount": float(row.get("minimum_order_amount") or 0),
        "per_customer_limit": int(row.get("per_customer_limit") or 0),
        "total_redemption_limit": int(row.get("total_redemption_limit") or 0),
        "stackable": bool(row.get("stackable")),
        "days_of_week": row.get("days_of_week") or "",
        "daily_start_time": str(daily_start or ""),
        "daily_end_time": str(daily_end or ""),
        "promo_code": row.promo_code or "",
        "badge_text": row.badge_text or "",
        "button_text": row.button_text or "Order now",
        "restaurant": row.restaurant or "",
        "starts_on": str(row.starts_on or ""),
        "ends_on": str(row.ends_on or ""),
        "active": bool(row.active),
    }
    payload.update(stats or {})
    return payload


def _offer_is_live(row, restaurant, when=None):
    when = when or now_datetime()
    if not row or not row.get("active"):
        return False
    if row.get("restaurant") and row.restaurant != restaurant:
        return False
    if row.get("starts_on") and row.starts_on > when:
        return False
    if row.get("ends_on") and row.ends_on < when:
        return False
    days = {day.title()[:3] for day in _csv_values(row.get("days_of_week"))}
    if days and when.strftime("%a") not in days:
        return False
    now_time = when.time()
    start, end = _daily_window(row)
    if start and end:
        if start <= end and not (start <= now_time <= end):
            return False
        if start > end and not (now_time >= start or now_time <= end):
            return False
    elif start and now_time < start:
        return False
    elif end and now_time > end:
        return False
    return True


def _offer_stats(names):
    stats = {name: {"views": 0, "claims": 0, "redemptions": 0,
                    "discount_given": 0.0, "revenue_generated": 0.0}
             for name in names}
    if not names:
        return stats
    event_rows = frappe.get_all(
        "Customer Offer Event",
        filters={"offer": ["in", names], "event_type": ["in", ["View", "Claim"]]},
        fields=["offer", "event_type", "count(name) as count"],
        group_by="offer, event_type",
        limit_page_length=0,
    ) if frappe.db.exists("DocType", "Customer Offer Event") else []
    for row in event_rows:
        stats[row.offer]["views" if row.event_type == "View" else "claims"] = int(row.count or 0)
    redemption_rows = frappe.db.sql(
        """
        SELECT offer, COUNT(*) AS redemptions,
               COALESCE(SUM(discount_amount), 0) AS discount_given,
               COALESCE(SUM(order_total), 0) AS revenue_generated
          FROM `tabCustomer Offer Redemption`
         WHERE offer IN %(offers)s AND status = 'Redeemed'
         GROUP BY offer
        """,
        {"offers": names},
        as_dict=True,
    ) if frappe.db.exists("DocType", "Customer Offer Redemption") else []
    for row in redemption_rows:
        stats[row.offer].update({
            "redemptions": int(row.redemptions or 0),
            "discount_given": float(row.discount_given or 0),
            "revenue_generated": float(row.revenue_generated or 0),
        })
    return stats


def _redemption_count(offer, mobile=None):
    filters = {"offer": offer, "status": ["in", ["Applied", "Redeemed"]]}
    if mobile:
        filters["mobile"] = mobile
    return frappe.db.count("Customer Offer Redemption", filters) if frappe.db.exists(
        "DocType", "Customer Offer Redemption"
    ) else 0


def _eligible_lines(offer, items):
    explicit = _csv_values(offer.get("eligible_items"))
    group = str(offer.get("eligible_item_group") or "").strip()
    item_groups = {}
    if group and not explicit:
        codes = [str(item.get("item") or "") for item in items]
        item_groups = {
            row.name: row.item_group
            for row in frappe.get_all(
                "Item", filters={"name": ["in", codes]},
                fields=["name", "item_group"], limit_page_length=0,
            )
        } if codes else {}
    return [
        item for item in items
        if (not explicit and not group)
        or (explicit and item.get("item") in explicit)
        or (group and not explicit and item_groups.get(item.get("item")) == group)
    ]


def prepare_customer_offer(offer_name, mobile, restaurant, items, subtotal):
    """Validate and price one selected offer without trusting the client."""
    offer_name = str(offer_name or "").strip()
    if not offer_name:
        return {"applied": False}
    if not frappe.db.exists("Customer Offer", offer_name):
        return {"applied": False, "error": "Offer is no longer available"}
    frappe.db.sql("SELECT name FROM `tabCustomer Offer` WHERE name=%s FOR UPDATE", offer_name)
    offer = frappe.get_doc("Customer Offer", offer_name)
    if not _offer_is_live(offer, restaurant):
        return {"applied": False, "error": "Offer is not active right now"}
    if offer.offer_type == "Announcement":
        return {"applied": False, "error": "This offer has no checkout discount"}
    minimum = float(offer.get("minimum_order_amount") or 0)
    if float(subtotal or 0) < minimum:
        return {"applied": False, "error": f"Spend at least Rs {minimum:.0f} for this offer"}
    per_customer = int(offer.get("per_customer_limit") or 0)
    total_limit = int(offer.get("total_redemption_limit") or 0)
    if per_customer and mobile and _redemption_count(offer.name, mobile) >= per_customer:
        return {"applied": False, "error": "You have already used this offer"}
    if total_limit and _redemption_count(offer.name) >= total_limit:
        return {"applied": False, "error": "This offer has reached its redemption limit"}

    eligible = _eligible_lines(offer, items)
    eligible_subtotal = sum(float(i.get("rate") or 0) * int(i.get("qty") or 0)
                            for i in eligible)
    result = {
        "applied": False,
        "offer": offer.name,
        "title": offer.title,
        "stackable": bool(offer.get("stackable")),
        "order_discount": 0.0,
        "benefit_amount": 0.0,
        "free_item": "",
        "free_quantity": 0,
    }
    if offer.offer_type == "Percentage Off":
        if eligible_subtotal <= 0:
            return {"applied": False, "error": "No eligible items are in this order"}
        amount = round(eligible_subtotal * float(offer.discount_percent or 0) / 100, 2)
        if amount <= 0:
            return {"applied": False, "error": "This offer has no discount configured"}
        result.update(applied=True, order_discount=amount, benefit_amount=amount)
    elif offer.offer_type == "Free Item":
        if not offer.free_item or not frappe.db.exists("Item", offer.free_item):
            return {"applied": False, "error": "The free item is not configured"}
        rate = float(frappe.db.get_value("Item Price", {
            "item_code": offer.free_item, "selling": 1,
        }, "price_list_rate") or frappe.db.get_value("Item", offer.free_item, "standard_rate") or 0)
        result.update(applied=True, free_item=offer.free_item, free_quantity=1,
                      benefit_amount=rate)
    elif offer.offer_type == "Buy X Get Y Free":
        buy = max(int(offer.buy_quantity or 0), 1)
        free_each = max(int(offer.free_quantity or 0), 1)
        eligible_qty = sum(int(i.get("qty") or 0) for i in eligible)
        sets = eligible_qty // buy
        if sets < 1:
            return {"applied": False,
                    "error": f"Add {buy - eligible_qty} more eligible item(s) for this offer"}
        cheapest = min(eligible, key=lambda item: float(item.get("rate") or 0)) if eligible else None
        if not cheapest:
            return {"applied": False, "error": "No eligible items are in this order"}
        free_qty = sets * free_each
        rate = float(cheapest.get("rate") or 0)
        result.update(applied=True, free_item=cheapest.get("item"),
                      free_quantity=free_qty, benefit_amount=round(rate * free_qty, 2))
    return result


def record_customer_offer(result, pos_order):
    if not result.get("applied"):
        return None
    redemption = frappe.get_doc({
        "doctype": "Customer Offer Redemption",
        "offer": result["offer"],
        "mobile": pos_order.mobile or "",
        "pos_order": pos_order.name,
        "status": "Applied",
        "discount_amount": result.get("benefit_amount") or 0,
        "free_item": result.get("free_item") or "",
        "free_quantity": result.get("free_quantity") or 0,
        "order_total": pos_order.grand_total or 0,
        "notes": result.get("title") or "",
    }).insert(ignore_permissions=True)
    return redemption.name


def _record_offer_event(offer, event_type, visitor_id="", pos_order="",
                        amount=0, revenue=0):
    if not frappe.db.exists("Customer Offer", offer):
        return False
    raw = f"{offer}|{event_type}|{visitor_id}|{pos_order}|{today()}"
    event_key = hashlib.sha256(raw.encode()).hexdigest()
    if frappe.db.exists("Customer Offer Event", event_key):
        return False
    frappe.get_doc({
        "doctype": "Customer Offer Event",
        "event_key": event_key,
        "offer": offer,
        "event_type": event_type,
        "visitor_hash": hashlib.sha256(str(visitor_id or "").encode()).hexdigest()[:24],
        "pos_order": pos_order or "",
        "amount": amount or 0,
        "revenue": revenue or 0,
    }).insert(ignore_permissions=True)
    return True


def finalize_offer_redemption(order_name, invoice_name=""):
    rows = frappe.get_all("Customer Offer Redemption", filters={
        "pos_order": order_name, "status": "Applied",
    }, fields=["name", "offer", "discount_amount"], limit_page_length=10)
    if not rows:
        return
    revenue = float(frappe.db.get_value("POS Order", order_name, "grand_total") or 0)
    for row in rows:
        frappe.db.set_value("Customer Offer Redemption", row.name,
                            {"status": "Redeemed", "order_total": revenue})
        _record_offer_event(row.offer, "Redeem", pos_order=order_name,
                            amount=row.discount_amount, revenue=revenue)


def reverse_offer_redemption(order_name):
    rows = frappe.get_all("Customer Offer Redemption", filters={
        "pos_order": order_name, "status": ["in", ["Applied", "Redeemed"]],
    }, fields=["name", "offer", "discount_amount"], limit_page_length=10)
    for row in rows:
        frappe.db.set_value("Customer Offer Redemption", row.name, "status", "Reversed")
        _record_offer_event(row.offer, "Reverse", pos_order=order_name,
                            amount=row.discount_amount)


@frappe.whitelist(allow_guest=True)
def customer_active_offers():
    """Public, display-safe active campaigns for one customer-app branch."""
    if not frappe.db.exists("DocType", "Customer Offer"):
        return {"success": True, "offers": []}
    requested = str(frappe.local.form_dict.get("restaurant") or "").strip()
    restaurant = requested if frappe.db.exists("Restaurant", requested) else branch.viewing()
    now = now_datetime()
    rows = frappe.db.sql(
        """
        SELECT *
          FROM `tabCustomer Offer`
         WHERE active = 1
           AND (restaurant IS NULL OR restaurant = '' OR restaurant = %(restaurant)s)
           AND (starts_on IS NULL OR starts_on <= %(now)s)
           AND (ends_on IS NULL OR ends_on >= %(now)s)
         ORDER BY creation DESC
         LIMIT 20
        """,
        {"restaurant": restaurant or "", "now": now},
        as_dict=True,
    )
    visible = []
    for row in rows:
        if not _offer_is_live(row, restaurant, now):
            continue
        limit = int(row.get("total_redemption_limit") or 0)
        if limit and _redemption_count(row.name) >= limit:
            continue
        visible.append(_offer_payload(row))
    return {"success": True, "offers": visible}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def customer_track_offer_event():
    data = frappe.local.form_dict
    offer = str(data.get("offer") or "").strip()
    event_type = str(data.get("event_type") or "").title()
    visitor_id = str(data.get("visitor_id") or "").strip()[:80]
    if event_type not in {"View", "Claim"} or len(visitor_id) < 8:
        return {"success": False}
    return {"success": True,
            "recorded": _record_offer_event(offer, event_type, visitor_id=visitor_id)}


@frappe.whitelist()
def manager_get_offers():
    roles.require_manager()
    if not frappe.db.exists("DocType", "Customer Offer"):
        return {"success": True, "offers": [], "restaurant": branch.viewing() or ""}
    restaurant = branch.viewing() or ""
    rows = frappe.get_all(
        "Customer Offer",
        filters={"restaurant": ["in", ["", restaurant]]},
        fields=["*"],
        order_by="modified desc",
        limit_page_length=100,
    )
    stats = _offer_stats([row.name for row in rows])
    return {
        "success": True,
        "restaurant": restaurant,
        "offers": [_offer_payload(row, stats.get(row.name)) for row in rows],
        "item_groups": frappe.get_all(
            "Item Group", filters={"is_group": 0}, pluck="name",
            order_by="name", limit_page_length=500,
        ),
        "items": frappe.get_all(
            "Item", filters={"disabled": 0, "is_sales_item": 1},
            fields=["name", "item_name", "item_group"],
            order_by="item_name", limit_page_length=1000,
        ),
    }


@frappe.whitelist(methods=["POST"])
def manager_save_offer():
    roles.require_manager()
    data = frappe.local.form_dict
    docname = str(data.get("name") or "").strip()
    title = str(data.get("title") or "").strip()[:140]
    offer_type = str(data.get("offer_type") or "").strip()
    if not title:
        frappe.throw(_("Offer title is required"))
    if offer_type not in OFFER_TYPES:
        frappe.throw(_("Choose a valid offer type"))

    values = {
        "title": title,
        "description": str(data.get("description") or "").strip()[:500],
        "offer_type": offer_type,
        "buy_quantity": _clean_int(data.get("buy_quantity"), 5, 1),
        "free_quantity": _clean_int(data.get("free_quantity"), 1, 1),
        "discount_percent": _clean_float(data.get("discount_percent"), 0),
        "free_item": str(data.get("free_item") or "").strip(),
        "eligible_item_group": str(data.get("eligible_item_group") or "").strip(),
        "eligible_items": ",".join(sorted(_csv_values(data.get("eligible_items"))))[:1000],
        "minimum_order_amount": _clean_float(data.get("minimum_order_amount"), 0, 0, 10000000),
        "per_customer_limit": _clean_int(data.get("per_customer_limit"), 1, 0, 100000),
        "total_redemption_limit": _clean_int(data.get("total_redemption_limit"), 0, 0, 10000000),
        "stackable": 1 if str(data.get("stackable") or "0").lower() in {"1", "true"} else 0,
        "days_of_week": ",".join(sorted({d.title()[:3] for d in _csv_values(data.get("days_of_week"))
                                           if d.title()[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}})),
        "daily_start_time": data.get("daily_start_time") or None,
        "daily_end_time": data.get("daily_end_time") or None,
        "promo_code": str(data.get("promo_code") or "").strip().upper()[:40],
        "badge_text": str(data.get("badge_text") or "").strip()[:80],
        "button_text": str(data.get("button_text") or "Order now").strip()[:40],
        "starts_on": data.get("starts_on") or None,
        "ends_on": data.get("ends_on") or None,
        "active": 1 if str(data.get("active", "1")).lower() not in {"0", "false"} else 0,
    }
    if values["free_item"] and not frappe.db.exists("Item", values["free_item"]):
        frappe.throw(_("Choose a valid free item"))
    if values["eligible_item_group"] and not frappe.db.exists(
            "Item Group", values["eligible_item_group"]):
        frappe.throw(_("Choose a valid eligible category"))
    for item_code in _csv_values(values["eligible_items"]):
        if not frappe.db.exists("Item", item_code):
            frappe.throw(_(f"Unknown eligible item: {item_code}"))
    if values["starts_on"] and values["ends_on"] and values["ends_on"] < values["starts_on"]:
        frappe.throw(_("End time must be after start time"))

    if docname:
        if not frappe.db.exists("Customer Offer", docname):
            frappe.throw(_("Offer not found"))
        doc = frappe.get_doc("Customer Offer", docname)
        current = branch.viewing() or ""
        if doc.restaurant and current and doc.restaurant != current and not roles.is_owner():
            frappe.throw(_("You cannot edit another restaurant's offer"), frappe.PermissionError)
        doc.update(values)
    else:
        offer_name = str(data.get("offer_name") or title).strip()[:140]
        if frappe.db.exists("Customer Offer", offer_name):
            frappe.throw(_("An offer with this name already exists"))
        doc = frappe.get_doc({
            "doctype": "Customer Offer",
            "offer_name": offer_name,
            "restaurant": branch.viewing() or "",
            **values,
        })
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    return {"success": True, "offer": _offer_payload(doc)}


@frappe.whitelist(methods=["POST"])
def manager_toggle_offer():
    roles.require_manager()
    name = str(frappe.local.form_dict.get("name") or "").strip()
    if not name or not frappe.db.exists("Customer Offer", name):
        frappe.throw(_("Offer not found"))
    doc = frappe.get_doc("Customer Offer", name)
    current = branch.viewing() or ""
    if doc.restaurant and current and doc.restaurant != current and not roles.is_owner():
        frappe.throw(_("You cannot edit another restaurant's offer"), frappe.PermissionError)
    active = 1 if str(frappe.local.form_dict.get("active") or "0") == "1" else 0
    doc.db_set("active", active)
    return {"success": True, "name": name, "active": bool(active)}
