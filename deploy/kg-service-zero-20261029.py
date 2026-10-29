import frappe,json
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm.api.pricing import _get_service_charge_rate
 before=frappe.get_all('Restaurant',filters={'name':['!=','Kurunegala']},fields=['name','service_charge'],order_by='name')
 doc=frappe.get_doc('Restaurant','Kurunegala')
 old=doc.service_charge
 doc.service_charge=0
 doc.save(ignore_permissions=True)
 frappe.set_user('luuvfryxokgala@gmail.com')
 assert _get_service_charge_rate()==0
 assert before==frappe.get_all('Restaurant',filters={'name':['!=','Kurunegala']},fields=['name','service_charge'],order_by='name')
 frappe.db.commit()
 print(json.dumps({'restaurant':'Kurunegala','previous_rate':old,'new_rate':0,'other_branch_rates_unchanged':before},default=str))
finally:frappe.db.rollback();frappe.destroy()
