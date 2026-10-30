import frappe,json,pathlib
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import branch
 users={'Administrator':'Kurunegala','kasungunarathna37@gmail.com':'Luuv Fryxo'}
 state={'choices':{u:branch.selected(u) for u in users},'dambulla':frappe.db.get_value('Restaurant','Luuv Fryxo',['warehouse','cost_center'],as_dict=True)}
 backup=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/inventory-release-config-20261030.json')
 if not backup.exists():backup.write_text(json.dumps(state,default=str))
 # Keep the operator locations the user confirmed, despite older saved choices.
 for user,restaurant in users.items():branch.select(restaurant,user)
 # Reuse the existing Dambulla POS accounting dimensions; do not invent a warehouse.
 profile=frappe.get_doc('POS Profile','Dambulla')
 assert profile.restaurant=='Luuv Fryxo'
 doc=frappe.get_doc('Restaurant','Luuv Fryxo')
 changed={}
 for field in ['warehouse','cost_center']:
  assert profile.get(field)
  if not doc.get(field):doc.set(field,profile.get(field));changed[field]=profile.get(field)
 if changed:doc.save(ignore_permissions=True)
 for dt,f in [('POS Order Item','stock_consumed_qty'),('Purchase Bill','stock_entry'),('Stock Entry','luuv_pos_order'),('Stock Entry','luuv_purchase_bill')]:assert frappe.db.has_column(dt,f)
 frappe.db.commit();frappe.clear_cache()
 print(json.dumps({'operator_locations':users,'dambulla_existing_pos_dimensions_linked':changed}))
finally:frappe.db.rollback();frappe.destroy()
