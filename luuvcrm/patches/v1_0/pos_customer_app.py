"""Durable installation evidence and one invitation preference per POS ticket."""
import frappe


def execute():
    from luuvcrm import settings
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    if settings.passive():
        return
    frappe.reload_doc("zeloura", "doctype", "customer_app_contact")
    create_custom_fields({"POS Order": [{
        "fieldname": "customer_app_invite", "label": "Send App Installation SMS",
        "fieldtype": "Check", "default": "0", "no_copy": 1,
        "read_only": 1, "insert_after": "mobile",
    }]}, update=True)
    frappe.clear_cache(doctype="POS Order")
