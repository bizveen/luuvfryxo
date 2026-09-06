"""Shared helpers for the luuvcrm regression tests.

These tests protect the flows the team edits most often — manager approvals,
purchase bills, and POS order cancellation. They call the real api.py functions
the way the POS/manager pages do (through frappe.local.form_dict where the
endpoint reads it), so a regression in the endpoint is caught, not just in a
helper. FrappeTestCase rolls each test back, so nothing here persists.
"""

import frappe
from luuvcrm import api

TEST_ITEM = "_LFT-TEST-ITEM"
BUYING_PL = "Standard Buying"


def ensure_setup():
    """The custom doctypes are created at runtime; make sure they exist."""
    if not frappe.db.exists("DocType", "POS Approval"):
        api.setup_pos_approval()
    if not frappe.db.exists("DocType", "Purchase Bill"):
        api.setup_purchase_bill()


def as_pos_user():
    # Administrator is in POS_ROLES, so it passes _require_pos_role().
    frappe.set_user("Administrator")


def form(**kw):
    """Stand in for the HTTP request body the whitelisted endpoints read."""
    frappe.local.form_dict = frappe._dict(kw)


def make_item(code=TEST_ITEM):
    if not frappe.db.exists("Item", code):
        frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code,
                        "item_group": "Food", "stock_uom": "Nos", "is_stock_item": 0,
                        "include_item_in_manufacturing": 0}).insert(ignore_permissions=True)
    return code


def set_buying_price(item, rate):
    existing = frappe.db.get_value("Item Price", {"item_code": item, "price_list": BUYING_PL}, "name")
    if existing:
        frappe.db.set_value("Item Price", existing, "price_list_rate", float(rate))
    else:
        frappe.get_doc({"doctype": "Item Price", "item_code": item, "price_list": BUYING_PL,
                        "buying": 1, "price_list_rate": float(rate)}).insert(ignore_permissions=True)


def get_buying_price(item):
    return float(frappe.db.get_value("Item Price", {"item_code": item, "price_list": BUYING_PL},
                                     "price_list_rate") or 0)


def make_pos_order(items, kitchen_status="Pending", customer="Walk-in"):
    """items: list of {item, qty, rate}. Inserts a draft (docstatus 0) POS Order."""
    return frappe.get_doc({
        "doctype": "POS Order", "customer_name": customer, "kitchen_status": kitchen_status,
        "items": [{"item": i["item"], "qty": i.get("qty", 1), "rate": i.get("rate", 100)}
                  for i in items],
    }).insert(ignore_permissions=True)


def make_purchase_bill(items, requested_by="Guest", status="pending", vendor="Test Vendor"):
    """items: list of {item, qty, price, old_price}. Built directly (not via
    create_purchase_bill) so the requester can differ from the approver."""
    total = sum(float(i.get("qty") or 0) * float(i.get("price") or 0) for i in items)
    return frappe.get_doc({
        "doctype": "Purchase Bill", "vendor": vendor, "status": status,
        "requested_by": requested_by, "requested_at": api.now_datetime(), "total": total,
        "items": [{"item": i["item"], "item_name": i.get("item_name", i["item"]),
                   "uom": i.get("uom", "Nos"), "qty": i.get("qty", 1),
                   "price": i.get("price", 0), "old_price": i.get("old_price", 0)}
                  for i in items],
    }).insert(ignore_permissions=True)


def make_approval(**fields):
    fields.setdefault("doctype", "POS Approval")
    fields.setdefault("status", "pending")
    fields.setdefault("requested_by", frappe.session.user)
    fields.setdefault("requested_at", api.now_datetime())
    doc = frappe.get_doc(fields)
    doc.flags.ignore_permissions = True
    return doc.insert()
