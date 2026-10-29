import frappe,json
from unittest.mock import patch
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
email='luuvfryxokgala@gmail.com'
try:
 from luuvcrm import branch,roles
 from luuvcrm.api import shifts
 assert branch.assigned(email)==['Kurunegala']
 user=frappe.get_doc('User',email)
 assert 'Restaurant Manager' in [r.role for r in user.roles]
 user.add_roles('Cashier')
 frappe.clear_cache(user=email)
 frappe.set_user(email)
 assert branch.accessible_pos_profiles()==['Kurunegala']
 for dt in ['POS Opening Entry','POS Closing Entry','POS Invoice']:
  assert all(frappe.has_permission(dt,p) for p in ['read','create','write','submit']),dt
 frappe.local.form_dict=frappe._dict(pos_profile='Kurunegala',opening_balance='0')
 frappe.db.savepoint('shift_permission_test')
 with patch.object(shifts.activity,'log_activity'):
  result=shifts.pos_open_shift()
 name=result['shift']['name']
 assert frappe.db.get_value('POS Opening Entry',name,'docstatus')==1
 frappe.db.rollback(save_point='shift_permission_test')
 assert not frappe.db.exists('POS Opening Entry',name)
 try:branch.select('Luuv Fryxo')
 except (frappe.PermissionError,frappe.ValidationError):pass
 else:raise AssertionError('Cross-branch selection allowed')
 frappe.db.commit()
 print(json.dumps({'user':email,'roles':frappe.get_roles(email),'shift_insert_submit_test':'passed and rolled back','accessible_profiles':branch.accessible_pos_profiles(),'dambulla_blocked':True}))
except Exception:
 frappe.db.rollback();raise
finally:frappe.destroy()
