import frappe,json,pathlib
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
admins={'Administrator','praveen2too@gmail.com','kasungunarathna37@gmail.com'}
kg='luuvfryxokgala@gmail.com'
try:
 from luuvcrm import branch
 users=frappe.get_all('User',filters={'name':['!=','Guest']},pluck='name',limit_page_length=0)
 profile=frappe.get_doc('POS Profile','Kurunegala')
 backup=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/staff-branch-restrictions-20261030.json')
 if not backup.exists():backup.write_text(json.dumps({'permissions':frappe.get_all('User Permission',filters={'allow':'Restaurant'},fields=['*'],limit_page_length=0),'kg_profile':profile.as_dict()},default=str))
 changed=[]
 for user in users:
  if user in admins:continue
  target='Kurunegala' if user==kg else 'Luuv Fryxo'
  perms=frappe.get_all('User Permission',filters={'user':user,'allow':'Restaurant'},fields=['name','for_value','apply_to_all_doctypes'])
  assert all(p.for_value==target for p in perms),'Unexpected cross-branch permission; inspect '+user
  if not perms:
   frappe.get_doc({'doctype':'User Permission','user':user,'allow':'Restaurant','for_value':target,'apply_to_all_doctypes':1}).insert(ignore_permissions=True)
   changed.append(user)
  for p in perms:
   if not p.apply_to_all_doctypes:frappe.db.set_value('User Permission',p.name,'apply_to_all_doctypes',1)
  frappe.clear_cache(user=user)
 removed=[r.user for r in profile.applicable_for_users if r.user not in admins|{kg}]
 if removed:
  profile.set('applicable_for_users',[r for r in profile.applicable_for_users if r.user in admins|{kg}])
  profile.save(ignore_permissions=True)
 for user in users:
  if user in admins:continue
  target='Kurunegala' if user==kg else 'Luuv Fryxo'
  frappe.set_user(user)
  assert branch.current()==target and branch.viewing()==target
  assert not branch.can_choose()
  if user!=kg:
   assert 'Kurunegala' not in branch.accessible_pos_profiles()
   try:branch.select('Kurunegala')
   except (frappe.PermissionError,frappe.ValidationError):pass
   else:raise AssertionError('Cross-branch choice allowed: '+user)
 frappe.set_user('Administrator');frappe.db.commit()
 print(json.dumps({'admin_accounts':sorted(admins),'kurunegala_manager':kg,'new_dambulla_assignments':changed,'removed_kg_profile_assignments':removed,'restricted_accounts_verified':len(users)-len(admins)}))
except Exception:frappe.db.rollback();raise
finally:frappe.destroy()
