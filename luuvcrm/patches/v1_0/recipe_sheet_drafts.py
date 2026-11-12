"""Store the owner's seven recipe drafts without creating stock or active BOMs."""
def execute():
    import frappe
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    from luuvcrm import branch,recipe_sheets
    from luuvcrm.api.recipe_sheets import FIELD,stored
    create_custom_fields({'Warehouse':[dict(fieldname=FIELD,label='Recipe sheet draft history',
        fieldtype='Long Text',read_only=1,no_copy=1)]},update=True)
    warehouse=branch.warehouse('Kurunegala') if frappe.db.exists('Restaurant','Kurunegala') else None
    if not warehouse:return
    values=stored(warehouse,lock=True)
    for item,reference in recipe_sheets.references('Kurunegala').items():
        values.setdefault(item,[recipe_sheets.initial(reference)])
    frappe.db.set_value('Warehouse',warehouse,FIELD,frappe.as_json(values))
