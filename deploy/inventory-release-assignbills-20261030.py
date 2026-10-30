import frappe,json,pathlib
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 path=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/purchase-branch-assignment-20261030.json')
 if path.exists():snapshot=json.loads(path.read_text())
 else:
  bills=frappe.get_all('Purchase Bill',filters={'restaurant':['is','not set']},fields=['name','status','total','restaurant'],order_by='name',limit_page_length=0)
  assert len(bills)==26,'Unassigned bill count changed; review first'
  names={b.name for b in bills}
  approvals=[]
  for a in frappe.get_all('POS Approval',filters={'action':'purchase_bill'},fields=['name','restaurant','action_payload'],limit_page_length=0):
   payload=json.loads(a.action_payload or '{}')
   if payload.get('bill') in names:
    assert not a.restaurant or a.restaurant=='Luuv Fryxo'
    approvals.append({'name':a.name,'restaurant':a.restaurant})
  snapshot={'bills':bills,'approvals':approvals,'target':'Luuv Fryxo'}
  path.write_text(json.dumps(snapshot,default=str))
 for b in snapshot['bills']:
  doc=frappe.get_doc('Purchase Bill',b['name'])
  assert not doc.restaurant or doc.restaurant=='Luuv Fryxo'
  assert doc.status==b['status'] and float(doc.total or 0)==float(b['total'] or 0),'Bill changed during assignment'
  frappe.db.set_value('Purchase Bill',doc.name,'restaurant','Luuv Fryxo',update_modified=False)
 for a in snapshot['approvals']:
  frappe.db.set_value('POS Approval',a['name'],'restaurant','Luuv Fryxo',update_modified=False)
 frappe.db.commit()
 print(json.dumps({'assigned_bills':len(snapshot['bills']),'assigned_approvals':len(snapshot['approvals']),'restaurant':'Luuv Fryxo','amounts_status_stock_unchanged':True}))
finally:frappe.db.rollback();frappe.destroy()
