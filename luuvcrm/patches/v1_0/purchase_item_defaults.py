"""Durable purchase category choices for each branch."""
def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({'Warehouse': [dict(fieldname='luuv_purchase_categories',
        label='Purchase item category defaults',fieldtype='Long Text',read_only=1,no_copy=1)]},update=True)
    from luuvcrm.api.purchase_item_categories import seed_history
    seed_history()
