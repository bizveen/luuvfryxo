import frappe,json,runpy
frappe.init(site='fryxo');frappe.connect()
try:
 from luuvcrm import branch
 from luuvcrm.api import orders
 ns=orders.__dict__
 for user,expected in [('Administrator','Kurunegala'),('kasungunarathna37@gmail.com','Luuv Fryxo'),('luuvfryxokgala@gmail.com','Kurunegala')]:
  frappe.set_user(user)
  assert branch.viewing()==expected
  counts={}
  for method in ['get_ongoing_orders','get_kiosk_orders','get_completed_orders']:
   result=ns[method]()
   rows=result['orders']
   for row in rows:
    assert frappe.db.get_value('POS Order',row.name,'restaurant')==expected,(user,method,row.name)
   counts[method]=len(rows)
  print(json.dumps({'user':user,'restaurant':expected,'counts':counts,'cross_branch_orders':0}))
finally:frappe.db.rollback();frappe.destroy()
