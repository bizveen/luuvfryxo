import frappe,json
frappe.init(site='fryxo');frappe.connect()
try:
 from luuvcrm import roles,branch
 email='luuvfryxokgala@gmail.com'
 doc=frappe.get_doc('User',email)
 assert doc.enabled and doc.user_type=='System User'
 assert roles.is_manager(email) and not roles.is_owner(email)
 assert branch.assigned(email)==['Kurunegala']
 assert branch.accessible_pos_profiles(email)==['Kurunegala']
 assert frappe.db.exists('POS Profile User',{'parent':'Kurunegala','user':email})
 frappe.set_user(email)
 roles.require_manager()
 assert branch.current()=='Kurunegala'
 assert branch.invoice_filters()=={'cost_center':'Kurunegala - LF'}
 print('VERIFIED: enabled manager account; Kurunegala branch, POS and report cost-center scope.')
finally:frappe.db.rollback();frappe.destroy()
