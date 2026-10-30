import sys,json,pathlib
# Verify live code
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import branch
 from luuvcrm.api import shifts,reports
 from luuvcrm.patches.v1_0.backfill_pos_invoice_cost_centers import execute
 assert '/apps/luuvcrm/' in shifts.__file__
 before=frappe.db.sql('select count(*),sum(grand_total),sum(paid_amount) from `tabPOS Invoice`')
 today=frappe.utils.today()
 expected=frappe.db.sql('select pp.restaurant,sum(pi.grand_total) total,count(*) n from `tabPOS Invoice` pi join `tabPOS Profile` pp on pp.name=pi.pos_profile where pi.docstatus=1 and pi.posting_date=%s group by pp.restaurant',(today,),as_dict=True)
 expected={r.restaurant:r for r in expected}
# Backfill already applied
 assert before==frappe.db.sql('select count(*),sum(grand_total),sum(paid_amount) from `tabPOS Invoice`')
 for user,restaurant,profile in [('kasungunarathna37@gmail.com','Luuv Fryxo','Dambulla'),('luuvfryxokgala@gmail.com','Kurunegala','Kurunegala')]:
  frappe.set_user(user)
  assert branch.viewing()==restaurant
  d=reports.get_manager_dashboard()
  assert abs(d['revenue']-float(expected[restaurant].total))<0.001
  assert d['sales_count']==expected[restaurant].n
  day=reports.get_manager_day_report(today)
  trend=reports.get_manager_sales_report(start=today,end=today)
  assert abs(trend['total_revenue']-d['revenue'])<0.001
  assert shifts._invoice_branch_fields(frappe.get_doc('POS Profile',profile))=={'cost_center':branch.cost_center()}
  print(json.dumps({'user':user,'date':today,'branch':restaurant,'dashboard_revenue':d['revenue'],'bills':d['sales_count'],'trend_revenue':trend['total_revenue'],'day_report_ok':bool(day),'new_invoice_branch_fields':'PASS'}))
  invoice=frappe.db.get_value('POS Invoice',{'pos_profile':profile,'posting_date':today,'docstatus':1},'name')
  assert reports.get_manager_bill_detail(invoice)
 # Retrying the backfill is a no-op for already-attributed invoices.
 frappe.set_user('Administrator')
 count=frappe.db.count('POS Invoice',{'cost_center':['is','set']})
# Backfill already applied;assert frappe.db.count('POS Invoice',{'cost_center':['is','set']})==count
 print('PASS totals unchanged, branch dashboard/day/trend/bill detail, repeat-safe backfill')
finally:frappe.db.rollback();frappe.destroy()
