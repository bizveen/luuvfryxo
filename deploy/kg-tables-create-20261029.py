import frappe,json
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm.api import tables
 def others():
  return json.dumps(frappe.get_all('Restaurant Table',filters={'restaurant':['!=','Kurunegala']},fields=['*'],order_by='name',limit_page_length=0),default=str,sort_keys=True)
 before=others()
 existing=frappe.get_all('Restaurant Table',filters={'restaurant':'Kurunegala'},pluck='name')
 assert len(existing) in (0,5),'Unexpected existing tables; inspect before creating'
 created=[]
 if not existing:
  for n in range(5):
   doc=frappe.get_doc({'doctype':'Restaurant Table','restaurant':'Kurunegala','enabled':1,'no_of_seats':4,'minimum_seating':1}).insert(ignore_permissions=True)
   created.append(doc.name)
 assert others()==before,'Other branch tables changed'
 frappe.set_user('luuvfryxokgala@gmail.com')
 visible=tables.for_picker()
 assert len(visible)==5
 assert all(frappe.db.get_value('Restaurant Table',t.name,'restaurant')=='Kurunegala' for t in visible)
 frappe.db.commit()
 print(json.dumps({'created':created,'manager_visible':visible,'other_branch_tables_unchanged':True,'seats_per_table':4}))
except Exception:frappe.db.rollback();raise
finally:frappe.destroy()
