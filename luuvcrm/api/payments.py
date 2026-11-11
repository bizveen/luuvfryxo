"""Taking money: a walk-in sale, paying an existing order, and splitting a bill.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from luuvcrm import activity, branch, roles
from luuvcrm.api.guides import (
    GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE,
    _credit_guide_cashback,
    _finalize_guide_redeem,
    _parse_guide_redeem,
)
from luuvcrm.api.orders import _order_fully_paid, _require_cus_call_released
from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _get_service_charge_rate,
    _item_is_takeaway,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.schema import _clean_item_note, _ensure_item_notes_field
from luuvcrm.api.shifts import (
    _active_pos_profile_name,
    _append_service_charge_tax,
    _ensure_open_shift,
    _invoice_branch_fields,
    _ensure_order_type_field,
    _invoice_doctype_for,
    _link_invoice_to_order,
)


# ─── Place Order ─────────────────────────────────────────

def _set_cash_tender(invoice, payment_mode, tendered, grand_total):
    """Record cash tendered + change returned on the invoice for the receipt.
    Display-only custom fields (custom_tendered_amount / custom_change_returned) —
    no effect on the payment rows, paid_amount, or any GL/accounting."""
    try:
        t = float(tendered or 0)
    except (TypeError, ValueError):
        t = 0
    if payment_mode == "Cash" and t > (grand_total or 0):
        invoice.custom_tendered_amount = t
        invoice.custom_change_returned = round(t - grand_total, 2)
def _tour_stamp(raw):
    """The fields that book an invoice under a Tour Company, or {} for none.

    Validated before anything is created, so a company that was removed while
    the payment dialog was open fails the payment loudly rather than billing a
    sale nobody can find in the company's statement. The discount is copied, not
    linked: what the company was on the day is what the statement must say.
    No money changes here — the guest pays the full price either way.
    """
    name = " ".join(str(raw or "").split())
    if not name:
        return {}
    row = frappe.db.get_value("Tour Company", name, ["name", "discount_percent", "disabled"], as_dict=True) \
        if frappe.db.exists("DocType", "Tour Company") else None
    if not row or row.disabled:
        frappe.throw(_("{0} is not an active tour company").format(name))
    return {"tour_company": row.name, "tour_discount": float(row.discount_percent or 0)}


@frappe.whitelist()
def get_tour_company_options():
    """The companies the till can book a bill under: names only. The discount is
    the manager's business — a cashier picks a company, not a price."""
    roles.require_pos()
    if not frappe.db.exists("DocType", "Tour Company"):
        return {"companies": []}
    return {"companies": frappe.get_all("Tour Company", filters={"disabled": 0},
                                        order_by="company_name asc", pluck="name",
                                        limit_page_length=0)}


@frappe.whitelist(methods=["POST"])
def place_order():
    roles.require_pos()
    data = frappe.local.form_dict
    from luuvcrm.api.pos_inventory import FIELD as INVENTORY_MODE, creation_mode
    inventory_mode = creation_mode()

    items_raw = data.get("items")
    items = frappe.parse_json(items_raw) if isinstance(items_raw, str) else items_raw

    if not items or len(items) == 0:
        frappe.throw(_("At least one item is required"))

    customer_name = data.get("customer_name", "").strip() or "Walk-in"
    mobile = data.get("mobile", "").strip() or ""
    table = data.get("table", "")
    if table:
        branch.require_access("Restaurant Table", table)
    notes = data.get("notes", "").strip()
    order_source = data.get("order_source", "Walk-in")
    order_type = data.get("order_type", "")
    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))
    waiter_name = frappe.get_value("User", frappe.session.user, "full_name") or frappe.session.user
    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    _ensure_order_type_field()

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)
    invoice_items = []
    pos_items = []
    subtotal = 0
    sc_base = 0

    for item_data in items:
        item_code = item_data.get("item")
        qty = max(int(item_data.get("qty", 1)), 1)
        rate = _server_item_rate(item_code)
        item_ta = _item_is_takeaway(item_data, order_type)

        invoice_items.append({"item_code": item_code, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0})
        pos_items.append({"item": item_code, "qty": qty, "rate": rate, "takeaway": 1 if item_ta else 0,
                          "notes": _clean_item_note(item_data.get("notes"))})
        subtotal += rate * qty
        if not item_ta:
            sc_base += rate * qty

    sc_rate, sc_amount = _service_charge_on(sc_base)
    grand_total = subtotal + sc_amount

    # Guide balance redemption (guide eats, pays from cashback): validated
    # up-front so an invalid redemption fails the payment loudly.
    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), grand_total)
    net_due = round(grand_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

    tour = _tour_stamp(data.get("tour_company"))
    invoice = frappe.get_doc({
        **tour,
        **_invoice_branch_fields(pos_profile),
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order | Customer: {customer_name} | Phone: {mobile} | Table: {table}",
        "items": [],
        "payments": payments,
    })
    for inv_item in invoice_items:
        invoice.append("items", inv_item)

    if sc_amount > 0:
        _append_service_charge_tax(invoice, sc_amount)
    if redeem_amt > 0:
        # the redeemed part rides as a Net Total discount so the invoice's grand
        # total equals what the customer actually pays (POS Invoice requires
        # payments to cover the full grand total on submit)
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = redeem_amt
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), net_due)

    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()  # finalize the sale so it consolidates at shift close

    pos_order = frappe.get_doc({
        "doctype": "POS Order",
        "naming_series": "POS-",
        "customer_name": customer_name,
        "waiter_name": waiter_name,
        "mobile": mobile,
        "restaurant_table": table,
        "order_source": order_source,
        "kitchen_status": "Pending",
        INVENTORY_MODE: inventory_mode,
        "grand_total": grand_total,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "pos_invoice": invoice.name,
        "notes": notes,
        "items": [],
    })

    opening_entry_name = frappe.db.get_value("POS Opening Entry",
        {"status": "Open", "user": frappe.session.user,
         "pos_profile": pos_profile_name}, "name")
    if opening_entry_name:
        pos_order.pos_opening_entry = opening_entry_name

    _ensure_item_notes_field()
    for item in pos_items:
        item_name = frappe.db.get_value("Item", item["item"], "item_name") or item["item"]
        pos_order.append("items", {
            "item": item["item"],
            "item_name": item_name,
            "qty": item["qty"],
            "rate": item["rate"],
            "takeaway": item.get("takeaway", 0),
            "notes": _clean_item_note(item.get("notes")),
        })

    pos_order.flags.ignore_permissions = True
    pos_order.flags.ignore_links = True
    pos_order.insert()
    if order_type:
        pos_order.db_set("order_type", order_type)
    _apply_takeaway_service_charge(pos_order)
    from luuvcrm.api.inventory import consume_order_stock

    consume_order_stock(pos_order.name)

    _link_invoice_to_order(invoice.name, pos_order.name)

    _finalize_guide_redeem(redeem_guide, redeem_amt, pos_order.name, invoice.name)
    _credit_guide_cashback(data.get("guides"),
        grand_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE else subtotal,
        pos_order.name, invoice.name, items=pos_items)

    flag, note = activity.discount_flag(redeem_amt, grand_total)
    activity.log_activity(
        "order.place", label="Walk-in sale Rs %.0f (%s)" % (net_due, payment_mode),
        ref_doctype="POS Order", ref_name=pos_order.name, bill_no=invoice.name,
        table=table or None, amount=net_due, total_before=grand_total, total_after=net_due,
        flag_reason=flag, flag_note=note,
        detail={"payment_mode": payment_mode, "cash": cash_amount, "card": card_amount,
                "subtotal": subtotal, "service_charge": sc_amount,
                "guide_redeem": redeem_amt, "items": len(pos_items),
                "order_type": order_type, "source": order_source,
                "tour_company": tour.get("tour_company")})

    return {
        "name": pos_order.name,
        "invoice_name": invoice.name,
        "grand_total": grand_total,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate,
        "service_charge_amount": sc_amount,
        "table": table,
        "payment_mode": payment_mode,
        "customer_name": customer_name,
        "mobile": mobile,
        "order_type": order_type,
    }
# ─── Process Payment (create invoice from existing order) ─

@frappe.whitelist(methods=["POST"])
def process_payment():
    roles.require_pos()
    data = frappe.local.form_dict
    order_name = data.get("order_name")

    if not order_name:
        frappe.throw(_("Order name is required"))

    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.pos_invoice:
        frappe.throw(_("Payment already processed for this order"))
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not in draft state"))
    _require_cus_call_released(pos_order)
    if any(int(i.get("paid_qty") or 0) > 0 for i in pos_order.items):
        frappe.throw(_("This order has split payments. Use Split Bill to pay the remaining items."))

    # Normally this was already issued when the KOT fired. The idempotent call
    # also covers legacy/open tickets that reach payment without that browser
    # acknowledgement.
    from luuvcrm.api.inventory import consume_order_stock

    consume_order_stock(order_name)

    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))

    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)

    order_total = float(pos_order.grand_total or 0)
    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), order_total)
    net_due = round(order_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

    tour = _tour_stamp(data.get("tour_company"))
    invoice = frappe.get_doc({
        **tour,
        **_invoice_branch_fields(pos_profile),
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order: {order_name} | {payment_mode}",
        "items": [],
        "payments": payments,
    })

    for item in pos_order.items:
        invoice.append("items", {"item_code": item.item, "qty": item.qty, "rate": item.rate, "takeaway": int(item.get("takeaway") or 0)})

    if pos_order.service_charge_amount and pos_order.service_charge_amount > 0:
        _append_service_charge_tax(invoice, pos_order.service_charge_amount)
    total_disc = (float(pos_order.get("discount_amount") or 0)
                  + float(pos_order.get("reward_redeem_amount") or 0)
                  + redeem_amt)
    if total_disc > 0:
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = total_disc
    _set_cash_tender(invoice, payment_mode, data.get("tendered"), net_due)

    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()  # finalize the sale so it consolidates at shift close

    pos_order.db_set("pos_invoice", invoice.name)
    _link_invoice_to_order(invoice.name, order_name)

    # A full payment is the final customer-facing step in this installation:
    # KOT starts Cooking, Ready finishes kitchen work, and payment completes the
    # order.  Keeping it at Ready made paid app orders look active forever and
    # prevented the customer's completion/review prompt from opening.
    pos_order.db_set("kitchen_status", "Served")
    pos_order.db_set("docstatus", 1)

    _finalize_guide_redeem(redeem_guide, redeem_amt, order_name, invoice.name)
    from luuvcrm.api.loyalty import finalize_reward_redemption, _process_cashback
    from luuvcrm.api.offers import finalize_offer_redemption

    finalize_reward_redemption(order_name, invoice.name)
    finalize_offer_redemption(order_name, invoice.name)
    if pos_order.mobile and order_total > 0:
        _process_cashback(pos_order.mobile, order_name, order_total)
    _credit_guide_cashback(data.get("guides"),
        order_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE
        else order_total - float(pos_order.service_charge_amount or 0),
        order_name, invoice.name,
        items=[{"item": i.item, "qty": i.qty, "rate": i.rate} for i in pos_order.items])

    flag, note = activity.discount_flag(total_disc, order_total)
    activity.log_activity(
        "payment.process", label="Paid Rs %.0f (%s)" % (net_due, payment_mode),
        ref_doctype="POS Order", ref_name=order_name, bill_no=invoice.name,
        table=pos_order.get("restaurant_table"),
        amount=net_due, total_before=order_total, total_after=net_due,
        flag_reason=flag, flag_note=note,
        detail={"payment_mode": payment_mode, "cash": cash_amount, "card": card_amount,
                "discount": float(pos_order.get("discount_amount") or 0),
                "reward_redeem": float(pos_order.get("reward_redeem_amount") or 0),
                "guide_redeem": redeem_amt, "items": len(pos_order.items),
                "tour_company": tour.get("tour_company")})

    return {
        "name": order_name,
        "invoice_name": invoice.name,
        "grand_total": pos_order.grand_total,
        "service_charge_rate": pos_order.service_charge_rate or 0,
        "service_charge_amount": pos_order.service_charge_amount or 0,
        "payment_mode": payment_mode,
        "status": "Served",
    }
# ─── Split Bill (pay a subset of an order's items as its own bill) ─

@frappe.whitelist(methods=["POST"])
def pay_split():
    roles.require_pos()
    data = frappe.local.form_dict
    order_name = data.get("order_name")
    if not order_name:
        frappe.throw(_("Order name is required"))
    lines_raw = data.get("lines")
    lines = frappe.parse_json(lines_raw) if isinstance(lines_raw, str) else (lines_raw or [])

    branch.require_access("POS Order", order_name)
    pos_order = frappe.get_doc("POS Order", order_name)
    if pos_order.docstatus != 0:
        frappe.throw(_("Order is not open"))
    _require_cus_call_released(pos_order)

    payment_mode = data.get("payment_mode", "Cash")
    cash_amount = float(data.get("cash_amount", 0))
    card_amount = float(data.get("card_amount", 0))

    pos_profile_name = _active_pos_profile_name(data.get("pos_profile", ""))
    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    _ensure_open_shift(pos_profile_name)

    # One order row per item; validate requested qty against remaining (qty - paid_qty).
    order_rows = {it.item: it for it in pos_order.items}
    requested = {}
    for ln in lines:
        code = ln.get("item")
        qty = int(ln.get("qty") or 0)
        if qty <= 0:
            continue
        row = order_rows.get(code)
        if not row:
            frappe.throw(_("Item {0} is not on this order").format(code))
        remaining = int(row.qty or 0) - int(row.get("paid_qty") or 0)
        if requested.get(code, 0) + qty > remaining:
            frappe.throw(_("Only {0} of {1} left to bill").format(remaining, code))
        requested[code] = requested.get(code, 0) + qty
    if not requested:
        frappe.throw(_("Select at least one item to bill"))

    from luuvcrm.api.inventory import consume_order_stock

    consume_order_stock(order_name)

    sc_rate = _get_service_charge_rate()
    subtotal = 0
    sc_base = 0
    invoice_items = []
    for code, qty in requested.items():
        row = order_rows[code]
        rate = float(row.rate or 0)
        invoice_items.append({"item_code": code, "qty": qty, "rate": rate, "takeaway": 1 if row.get("takeaway") else 0})
        subtotal += rate * qty
        if not row.get("takeaway"):
            sc_base += rate * qty

    sc_amount = round(sc_base * sc_rate / 100, 2) if (sc_rate > 0 and sc_base > 0) else 0.0
    grand_total = subtotal + sc_amount

    redeem_guide, redeem_amt = _parse_guide_redeem(data.get("guide_redeem"), grand_total)
    net_due = round(grand_total - redeem_amt, 2)

    payments = []
    if payment_mode == "Cash+Card":
        if cash_amount > 0:
            payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
        if card_amount > 0:
            payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})
    else:
        payments.append({"mode_of_payment": payment_mode, "amount": net_due})

    tour = _tour_stamp(data.get("tour_company"))
    invoice = frappe.get_doc({
        **tour,
        **_invoice_branch_fields(pos_profile),
        "doctype": _invoice_doctype_for(pos_profile_name),
        "is_pos": 1,
        "pos_profile": pos_profile_name,
        "customer": "Walk In",
        "company": pos_profile.company,
        "currency": pos_profile.currency or "LKR",
        "selling_price_list": pos_profile.selling_price_list or "",
        "set_warehouse": pos_profile.warehouse or "",
        "update_stock": 0,
        "posting_date": now_datetime().strftime("%Y-%m-%d"),
        "remarks": f"POS Order: {order_name} | SPLIT | {payment_mode}",
        "items": [],
        "payments": payments,
    })
    for inv_item in invoice_items:
        invoice.append("items", inv_item)
    if sc_amount > 0:
        _append_service_charge_tax(invoice, sc_amount)
    if redeem_amt > 0:
        invoice.apply_discount_on = "Net Total"
        invoice.discount_amount = redeem_amt
    invoice.flags.ignore_permissions = True
    invoice.insert()
    if invoice.doctype == "POS Invoice":
        invoice.submit()

    for code, qty in requested.items():
        row = order_rows[code]
        row.db_set("paid_qty", int(row.get("paid_qty") or 0) + qty)

    pos_order.reload()
    fully_paid = _order_fully_paid(pos_order)
    if fully_paid:
        if not pos_order.pos_invoice:
            pos_order.db_set("pos_invoice", invoice.name)
        _link_invoice_to_order(invoice.name, order_name)
        pos_order.db_set("docstatus", 1)
        pos_order.db_set("kitchen_status", "Served")
        from luuvcrm.api.loyalty import finalize_reward_redemption, _process_cashback
        from luuvcrm.api.offers import finalize_offer_redemption

        finalize_reward_redemption(order_name, invoice.name)
        finalize_offer_redemption(order_name, invoice.name)
        if pos_order.mobile and float(pos_order.grand_total or 0) > 0:
            _process_cashback(pos_order.mobile, order_name,
                              float(pos_order.grand_total or 0))

    _finalize_guide_redeem(redeem_guide, redeem_amt, order_name, invoice.name)
    _credit_guide_cashback(data.get("guides"),
        grand_total if GUIDE_CASHBACK_INCLUDES_SERVICE_CHARGE else subtotal,
        order_name, invoice.name,
        items=[{"item": ii["item_code"], "qty": ii["qty"], "rate": ii["rate"]}
               for ii in invoice_items])

    flag, note = activity.discount_flag(redeem_amt, grand_total)
    activity.log_activity(
        "payment.split", label="Split payment Rs %.0f (%s)" % (net_due, payment_mode),
        ref_doctype="POS Order", ref_name=order_name, bill_no=invoice.name,
        table=pos_order.get("restaurant_table"),
        amount=net_due, total_before=grand_total, total_after=net_due,
        flag_reason=flag, flag_note=note,
        detail={"payment_mode": payment_mode, "lines": len(invoice_items),
                "guide_redeem": redeem_amt, "fully_paid": bool(fully_paid),
                "tour_company": tour.get("tour_company")})

    return {
        "name": order_name,
        "invoice_name": invoice.name,
        "subtotal": subtotal,
        "service_charge_rate": sc_rate if sc_amount > 0 else 0,
        "service_charge_amount": sc_amount,
        "grand_total": grand_total,
        "payment_mode": payment_mode,
        "fully_paid": fully_paid,
    }
