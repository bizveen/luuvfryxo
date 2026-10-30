import sys,json
sys.path.insert(0,'/tmp/luuv-inventory-release-20261030')
import frappe
from unittest.mock import patch
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import branch,activity
 from luuvcrm.api import inventory,orders,purchases
 from erpnext.stock.utils import get_stock_balance
 token='LFR30-'+frappe.generate_hash(length=6)
 raw,meal,prepared=[token+x for x in ['-RAW','-MEAL','-PREP']]
 warehouse=frappe.db.get_value('Restaurant','Kurunegala','warehouse')
 company=frappe.db.get_value('Warehouse',warehouse,'company')
 group=frappe.db.get_value('Item Group',{'is_group':0},'name')
 with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
  for code,stock in [(raw,1),(meal,0),(prepared,1)]:
   frappe.get_doc({'doctype':'Item','item_code':code,'item_name':code,'item_group':group,'stock_uom':('Kg' if stock else 'Nos'),'is_stock_item':stock,'is_purchase_item':1,'is_sales_item':1}).insert(ignore_permissions=True)
  receipt=inventory._new_stock_entry('Material Receipt',company,warehouse,'Rollback release verification')
  receipt.append('items',{'item_code':raw,'t_warehouse':warehouse,'qty':10,'uom':'Kg','conversion_factor':1,'basic_rate':100,'set_basic_rate_manually':1,'cost_center':branch.cost_center()})
  inventory._submit(receipt)
  assert float(get_stock_balance(raw,warehouse))==10
  inventory.save_prepared_recipe(item_code=prepared,ingredients=[{'item':raw,'qty':0.5}],restaurant='Kurunegala')
  made=inventory.prepare_item_batch(item_code=prepared,qty=4,restaurant='Kurunegala')
  assert float(get_stock_balance(raw,warehouse))==8
  assert float(get_stock_balance(prepared,warehouse))==4
  inventory.save_menu_recipe(item_code=meal,ingredients=[{'item':prepared,'qty':0.25},{'item':raw,'qty':0.1}],restaurant='Kurunegala')
  frappe.local.form_dict=frappe._dict(items=json.dumps([{'item':meal,'qty':2}]),order_type='Take Away',order_source='Walk-in',draft=0)
  with patch.object(orders,'_server_item_rate',return_value=1000):
   order=orders.send_to_kitchen()
  assert abs(float(get_stock_balance(raw,warehouse))-7.8)<0.00001
  assert float(get_stock_balance(prepared,warehouse))==3.5
  again=inventory.consume_order_stock(order['name'])
  assert again['consumed']==0
  entry=frappe.get_doc('Stock Entry',frappe.db.get_value('Stock Entry',{'luuv_pos_order':order['name']},'name'))
  assert all(r.cost_center==branch.cost_center() for r in entry.items)
  print('PASS native receipt, submitted BOMs, prepared batch, kitchen consumption, retry idempotence, branch cost centre')
  # The purchase path must receive once and retain its link for a retry.
  bill=frappe.get_doc({'doctype':'Purchase Bill','restaurant':'Kurunegala','vendor':'Release rollback verification','status':'pending','purchase_date':frappe.utils.today(),'requested_by':'Administrator','items':[{'item':raw,'qty':3,'price':100,'uom':'Kg'}]})
  bill.insert(ignore_permissions=True,ignore_mandatory=True)
  received=inventory.receive_purchase_stock(bill)
  assert received['received']==3
  assert inventory.receive_purchase_stock(bill)['received']==0
  assert abs(float(get_stock_balance(raw,warehouse))-10.8)<0.00001
  print('PASS purchase stock receipt and repeat protection')
 frappe.db.rollback()
 assert not frappe.db.exists('Item',raw)
 print('PASS all test records rolled back; no production stock changes')
finally:frappe.db.rollback();frappe.destroy()
