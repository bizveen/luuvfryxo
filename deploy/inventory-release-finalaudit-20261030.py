import frappe,json,pathlib
frappe.init(site='fryxo');frappe.connect()
try:
 from luuvcrm import branch
 from luuvcrm.api.purchases import get_purchase_bills
 snapshot=json.loads(pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/purchase-branch-assignment-20261030.json').read_text())
 for b in snapshot['bills']:
  row=frappe.db.get_value('Purchase Bill',b['name'],['restaurant','status','total'],as_dict=True)
  assert row.restaurant=='Luuv Fryxo' and row.status==b['status'] and float(row.total or 0)==float(b['total'] or 0)
 for u,expected in [('Administrator','Kurunegala'),('kasungunarathna37@gmail.com','Luuv Fryxo')]:
  frappe.set_user(u)
  assert branch.viewing()==expected
  result=get_purchase_bills()
  for b in result['bills']:assert frappe.db.get_value('Purchase Bill',b['name'],'restaurant')==expected
  assert branch.invoice_filters().get('cost_center')==('Main - LF' if expected=='Luuv Fryxo' else 'Kurunegala - LF')
  print(json.dumps({'user':u,'restaurant':expected,'visible_purchase_bills':len(result['bills']),'invoice_filter':branch.invoice_filters()}))
 print('PASS all 26 assigned bills preserve amounts/status; purchase lists and invoice filters stay branch-scoped')
finally:frappe.db.rollback();frappe.destroy()
