"""Schema for manager recipe versions and append-only stock-count annotations.

Prepared locally; run during a reviewed real-app release before enabling the UI.
No opening quantities or automatic recipe activation are created here.
"""
def execute():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({
        'Restaurant': [
            dict(fieldname='luuv_pos_stock_policy',label='POS stock shortage rules',fieldtype='Long Text',read_only=1,no_copy=1),
        ],
        'Stock Entry': [
            dict(fieldname='luuv_stock_policy_snapshot',label='POS stock rules at posting',fieldtype='Long Text',read_only=1,no_copy=1),
        ],
        'Warehouse': [
            dict(fieldname='luuv_stock_minimums',label='Manager stock minimums',fieldtype='Long Text',read_only=1,no_copy=1),
        ],
        'BOM': [
            dict(fieldname='luuv_recipe_restaurant',label='Recipe branch',fieldtype='Link',options='Restaurant',read_only=1,no_copy=1),
            dict(fieldname='luuv_recipe_effective_from',label='Recipe effective from',fieldtype='Datetime',read_only=1,no_copy=1),
        ],
        'Stock Reconciliation': [
            dict(fieldname='luuv_audit_restaurant',label='Audit branch',fieldtype='Link',options='Restaurant',read_only=1,no_copy=1),
            dict(fieldname='luuv_audit_details',label='Stock count record',fieldtype='Long Text',read_only=1,no_copy=1),
        ],
    },update=True)
