"""Install draft storage and import the seven supplied sheets, idempotently."""
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,recipe_sheets
    from luuvcrm.api.recipe_sheets import stored,FIELD
    from luuvcrm.patches.v1_0.recipe_sheet_drafts import execute
    codes=list(recipe_sheets.references('Kurunegala'))
    def boms():return frappe.get_all('BOM',filters={'item':['in',codes]},fields=['name','docstatus','is_default'],order_by='name',limit_page_length=0)
    before=boms()
    execute()
    warehouse=branch.warehouse('Kurunegala');values=stored(warehouse)
    assert set(codes)<=set(values)
    assert sum(len(values[code][0]['ingredients']) for code in codes)==49
    assert boms()==before,'Existing BOMs changed unexpectedly'
    patch='luuvcrm.patches.v1_0.recipe_sheet_drafts'
    if not frappe.db.exists('Patch Log',{'patch':patch}):
        frappe.get_doc(dict(doctype='Patch Log',patch=patch)).insert(ignore_permissions=True)
    frappe.db.commit()
    print('Installed persistent draft storage: 7 supplied recipes, 49 ingredient rows. Existing BOMs unchanged.')
finally:frappe.db.rollback();frappe.destroy()
