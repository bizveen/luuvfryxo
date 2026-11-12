"""Enable branch preparation without posting stock or activating sample recipes."""
def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({
        'Warehouse':[dict(fieldname='luuv_prepared_items',label='Branch prepared items',fieldtype='Long Text',read_only=1,no_copy=1)],
        'BOM':[dict(fieldname='luuv_preparation_note',label='Preparation recipe note',fieldtype='Small Text',read_only=1,no_copy=1)],
        'Stock Entry':[
            dict(fieldname='luuv_preparation_request',label='Preparation request',fieldtype='Data',unique=1,read_only=1,no_copy=1),
            dict(fieldname='luuv_preparation_details',label='Saved preparation details',fieldtype='Long Text',read_only=1,no_copy=1),
        ],
    },update=True)
