"""The menu the POS caches, and the orders it replays when the wifi comes back.

Split out of the single 7,338-line api.py; every name here is re-exported
from `luuvcrm.api`, so the whitelisted method paths are unchanged.
"""

import frappe
from frappe.utils import now_datetime

from luuvcrm import activity, branch
from luuvcrm.api import tables
from luuvcrm.api.menu import menu_items

from luuvcrm.api.pricing import (
    _apply_takeaway_service_charge,
    _get_pos_price_list,
    _item_is_takeaway,
    _resolve_item_rate,
    _server_item_rate,
    _service_charge_on,
)
from luuvcrm.api.shifts import (
    _active_pos_profile_name,
    _append_service_charge_tax,
    _ensure_open_shift,
    _invoice_doctype_for,
    _link_invoice_to_order,
)


# ─── Offline Sync ─────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
def get_menu_data():
    # This is the payload the till caches to keep selling when the wifi drops,
    # so it is the one read that must not be a document per row: it is fetched
    # on every POS load and again by the service worker.
    items = menu_items(branch.viewing())
    # A till that goes offline with a disabled table in its cache would offer
    # it until the next sync, so the flag has to be applied here too.
    return {"items": items, "tables": tables.for_picker()}
@frappe.whitelist(methods=["POST"])
def sync_offline_orders():
    orders_raw = frappe.local.form_dict.get("orders")
    orders = frappe.parse_json(orders_raw) if isinstance(orders_raw, str) else (orders_raw or [])
    results = []
    for o in orders:
        local_id = o.get("local_id", "")
        items = o.get("items", [])
        table = o.get("table", "")
        customer_name = o.get("customer_name", "").strip() or "Walk-in"
        mobile = o.get("mobile", "").strip() or ""
        notes = o.get("notes", "").strip()
        payment_mode = o.get("payment_mode", "Cash")
        cash_amount = float(o.get("cash_amount", 0))
        card_amount = float(o.get("card_amount", 0))
        order_type = o.get("order_type", "Dine In")
        pos_profile_name = _active_pos_profile_name(o.get("pos_profile", ""))

        if not items:
            results.append({"local_id": local_id, "status": "failed", "error": "No items"})
            continue

        try:
            rate_map = {}
            for item_data in items:
                code = item_data.get("item") or item_data.get("code")
                qty = max(int(item_data.get("qty", 1)), 1)
                rate = _server_item_rate(code)
                rate_map[code] = {"qty": qty, "rate": rate,
                                  "takeaway": 1 if _item_is_takeaway(item_data, order_type) else 0}

            invoice_items = []
            subtotal = 0
            sc_base = 0
            for code, ri in rate_map.items():
                invoice_items.append({"item_code": code, "qty": ri["qty"], "rate": ri["rate"], "takeaway": ri["takeaway"]})
                subtotal += ri["rate"] * ri["qty"]
                if not ri["takeaway"]:
                    sc_base += ri["rate"] * ri["qty"]

            sc_rate, sc_amount = _service_charge_on(sc_base)
            grand_total = subtotal + sc_amount

            payments = [{"mode_of_payment": payment_mode, "amount": grand_total}]
            if payment_mode == "Cash+Card":
                payments = []
                if cash_amount > 0:
                    payments.append({"mode_of_payment": "Cash", "amount": cash_amount})
                if card_amount > 0:
                    payments.append({"mode_of_payment": "Credit Card", "amount": card_amount})

            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
            _ensure_open_shift(pos_profile_name)

            invoice = frappe.get_doc({
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
                "remarks": f"Offline Synced | {customer_name} | Table: {table}",
                "items": [],
                "payments": payments,
            })
            for inv_item in invoice_items:
                invoice.append("items", inv_item)

            if sc_amount > 0:
                _append_service_charge_tax(invoice, sc_amount)

            invoice.flags.ignore_permissions = True
            invoice.insert()

            waiter_name = customer_name
            pos_order = frappe.get_doc({
                "doctype": "POS Order",
                "naming_series": "POS-",
                "customer_name": customer_name,
                "waiter_name": waiter_name,
                "mobile": mobile,
                "restaurant_table": table,
                "order_source": "Walk-in",
                "order_type": order_type,
                "kitchen_status": "Pending",
                "grand_total": grand_total,
                "service_charge_rate": sc_rate,
                "service_charge_amount": sc_amount,
                "pos_invoice": invoice.name,
                "notes": notes,
                "items": [],
            })
            for code, ri in rate_map.items():
                item_name = frappe.db.get_value("Item", code, "item_name") or code
                pos_order.append("items", {"item": code, "item_name": item_name, "qty": ri["qty"], "rate": ri["rate"], "takeaway": ri["takeaway"]})
            pos_order.flags.ignore_permissions = True
            pos_order.flags.ignore_links = True
            pos_order.insert()
            _apply_takeaway_service_charge(pos_order)
            from luuvcrm.api.inventory import consume_order_stock

            consume_order_stock(pos_order.name)

            _link_invoice_to_order(invoice.name, pos_order.name)

            activity.log_activity(
                "order.sync",
                label="Offline sale replayed Rs %.0f (%s)" % (grand_total, payment_mode),
                ref_doctype="POS Order", ref_name=pos_order.name, bill_no=invoice.name,
                table=table or None, amount=grand_total, total_after=grand_total,
                detail={"local_id": local_id, "payment_mode": payment_mode,
                        "items": len(rate_map), "order_type": order_type})

            results.append({
                "local_id": local_id,
                "status": "synced",
                "server_name": pos_order.name,
                "invoice_name": invoice.name,
                "grand_total": grand_total,
                "service_charge_rate": sc_rate,
                "service_charge_amount": sc_amount,
            })
        except Exception as e:
            frappe.log_error(f"Offline sync error for {local_id}: {str(e)}", "OfflineSync")
            # A sale the till believes it took and the server refused. Nothing
            # else keeps it: the loop swallows the exception and moves on.
            activity.log_activity(
                "order.sync", label="Offline sale failed to replay",
                table=table or None, ok=0, err=str(e),
                detail={"local_id": local_id, "items": len(items or [])})
            results.append({"local_id": local_id, "status": "failed", "error": str(e)})

    return {"results": results}
