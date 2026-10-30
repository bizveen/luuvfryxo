import frappe,json
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
email='nawishanpahasara@gmail.com'
try:
 from luuvcrm import branch,roles
 from luuvcrm.www import waiter
 assert frappe.db.get_value('User',email,'enabled')==1
 assigned=branch.assigned(email)
 assert not assigned or assigned==['Luuv Fryxo'],assigned
 user=frappe.get_doc('User',email)
 user.send_welcome_email=0
 if 'Waiter' not in [r.role for r in user.roles]:user.append('roles',{'role':'Waiter'})
 user.save(ignore_permissions=True)
 if not assigned:
  frappe.get_doc({'doctype':'User Permission','user':email,'allow':'Restaurant','for_value':'Luuv Fryxo','apply_to_all_doctypes':1}).insert(ignore_permissions=True)
 frappe.clear_cache(user=email)
 frappe.set_user(email);frappe.local.session.data=frappe._dict(csrf_token='verification')
 roles.require_waiter()
 assert branch.current()=='Luuv Fryxo'
 ctx=frappe._dict();waiter.get_context(ctx)
 assert ctx.tables and all(frappe.db.get_value('Restaurant Table',t.name,'restaurant')=='Luuv Fryxo' for t in ctx.tables)
 assert not branch.can_choose()
 try:branch.select('Kurunegala')
 except (frappe.PermissionError,frappe.ValidationError):pass
 else:raise AssertionError('Cross-branch choice allowed')
 frappe.db.commit()
 print(json.dumps({'user':email,'roles':frappe.get_roles(email),'branch':branch.current(),'waiter_page':ctx.restaurant_name,'table_count':len(ctx.tables),'kurunegala_selection_blocked':True,'pos_access':roles.has_any(roles.POS_ROLES)}))
except Exception:frappe.db.rollback();raise
finally:frappe.destroy()
