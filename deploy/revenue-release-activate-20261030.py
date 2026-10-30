import frappe,json,pathlib
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm.patches.v1_0.backfill_pos_invoice_cost_centers import execute
 path=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/invoice-branch-before-20261030.json')
 if not path.exists():
  snapshot={'invoices':frappe.get_all('POS Invoice',filters={'cost_center':['is','not set']},fields=['name','cost_center','pos_profile','grand_total','paid_amount'],limit_page_length=0),'items':frappe.get_all('POS Invoice Item',filters={'cost_center':['is','not set']},fields=['name','parent','cost_center'],limit_page_length=0)}
  path.write_text(json.dumps(snapshot,default=str))
 before=frappe.db.sql('select count(*),sum(grand_total),sum(paid_amount) from `tabPOS Invoice`')
 missing_before=frappe.db.count('POS Invoice',{'cost_center':['is','not set']})
 execute()
 assert before==frappe.db.sql('select count(*),sum(grand_total),sum(paid_amount) from `tabPOS Invoice`')
 missing_after=frappe.db.count('POS Invoice',{'cost_center':['is','not set']})
 patch='luuvcrm.patches.v1_0.backfill_pos_invoice_cost_centers'
 if not frappe.db.exists('Patch Log',{'patch':patch}):frappe.get_doc({'doctype':'Patch Log','patch':patch}).insert(ignore_permissions=True)
 frappe.db.commit();frappe.clear_cache()
 print(json.dumps({'invoices_attributed':missing_before-missing_after,'remaining_unattributed':missing_after,'amounts_and_counts_unchanged':True}))
finally:frappe.db.rollback();frappe.destroy()
