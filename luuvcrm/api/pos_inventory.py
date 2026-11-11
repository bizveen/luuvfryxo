"""Inventory is an order property, never inferred from a browser's current URL."""
from contextlib import contextmanager

import frappe
from frappe import _

from luuvcrm import branch, roles

FIELD = "luuv_inventory_pos"


def creation_mode(amended_from=None):
    mode = bool(frappe.flags.get("inventory_pos_create"))
    if amended_from:
        branch.require_access("POS Order", amended_from)
        mode = mode or bool(frappe.db.get_value("POS Order", amended_from, FIELD))
    if mode and not frappe.db.has_column("POS Order", FIELD):
        frappe.throw(_("Install the inventory POS schema before taking inventory orders."))
    return int(mode)


def protect_mode(doc, method=None):
    """A saved ticket keeps its inventory mode through edits and payments."""
    if not doc.is_new() and frappe.db.has_column("POS Order", FIELD):
        saved = int(frappe.db.get_value("POS Order", doc.name, FIELD) or 0)
        if int(doc.get(FIELD) or 0) != saved:
            frappe.throw(_("This order's inventory mode cannot be changed."))


def list_filter():
    """Only POS screens ask for a channel; kitchen/waiter lists remain shared."""
    channel = (frappe.local.form_dict or {}).get("pos_channel")
    if channel in ("legacy", "inventory") and frappe.db.has_column("POS Order", FIELD):
        return {FIELD: int(channel == "inventory")}
    return {}


@contextmanager
def inventory_creation():
    roles.require_pos()
    previous = frappe.flags.get("inventory_pos_create")
    frappe.flags.inventory_pos_create = True
    try:
        yield
    finally:
        frappe.flags.inventory_pos_create = previous


@frappe.whitelist(methods=["POST"])
def send_to_kitchen():
    from luuvcrm.api import orders
    with inventory_creation():
        return orders.send_to_kitchen()


@frappe.whitelist(methods=["POST"])
def place_order():
    from luuvcrm.api import payments
    with inventory_creation():
        return payments.place_order()


@frappe.whitelist(methods=["POST"])
def prepare_kot(order_name):
    """Check/issue before printing; a failed printer may retry the same issue."""
    roles.require_pos()
    branch.require_access("POS Order", order_name)
    order = frappe.get_doc("POS Order", order_name)
    if not order.get(FIELD):
        frappe.throw(_("Open this ordinary POS order in /pos."))
    if order.docstatus == 2 or order.kitchen_status == "Cancelled":
        frappe.throw(_("This order has been cancelled."))
    from luuvcrm.api.inventory import consume_order_stock
    return {"ok": True, "stock": consume_order_stock(order_name)}
