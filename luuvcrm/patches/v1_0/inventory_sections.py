"""Separate branch equipment lists without changing stock quantities or valuation."""


def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({'Warehouse': [dict(fieldname='luuv_main_inventory',
        label='Main inventory equipment', fieldtype='Long Text', read_only=1, no_copy=1)]}, update=True)
