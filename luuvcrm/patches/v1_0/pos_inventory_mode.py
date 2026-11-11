"""Existing tickets remain ordinary POS orders; no backfill or stock movement."""


def execute():
    from luuvcrm import settings
    if settings.passive():
        return
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({
        "POS Order": [dict(fieldname="luuv_inventory_pos", label="Inventory POS",
                           fieldtype="Check", default="0", read_only=1, no_copy=1)],
    }, update=True)
