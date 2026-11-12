"""Delivery evidence is independent of payment, kitchen and inventory state."""
def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({'POS Order':[dict(fieldname='luuv_delivery_state',label='Delivery confirmation history',
        fieldtype='Long Text',read_only=1,no_copy=1,allow_on_submit=1)]},update=True)
