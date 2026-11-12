"""Add the requested neutral purchase category without reclassifying any bills."""
import json
import frappe

frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    name='Not sure'
    existing=frappe.db.exists('Expense Category', {'category_name': name})
    if existing:
        doc=frappe.get_doc('Expense Category',existing)
        assert not doc.disabled, 'Existing category is disabled; inspect before changing it.'
    else:
        doc=frappe.get_doc(dict(doctype='Expense Category',category_name=name,
            in_stock=0,has_item_list=0,is_fixed_asset=0,disabled=0)).insert(ignore_permissions=True)
        frappe.db.commit()
    from luuvcrm.api.expenses import get_expense_categories
    result=next(c for c in get_expense_categories()['category_details'] if c['name']==doc.name)
    print(json.dumps(dict(created=not bool(existing),category=result)))
finally:
    frappe.db.rollback();frappe.destroy()
