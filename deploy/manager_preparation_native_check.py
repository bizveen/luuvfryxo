"""Native preparation checks; all temporary items, recipes and ledgers roll back."""
import sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-coleslaw-preparation-20261112/stage')
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager,manager_preparation as prep,inventory
    warehouse=branch.warehouse('Kurunegala')
    def blocked(fn,text):
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as e:
            assert text.lower() in str(e).lower(),str(e);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    with patch.object(branch,'selected',return_value='Kurunegala'),patch.object(frappe.db,'commit'),patch.object(frappe,'publish_realtime'),patch.object(frappe,'enqueue'),patch.object(activity,'log_activity'):
        items=[]
        for suffix in ('CABBAGE','CARROT','MAYO','SALAD','CUPS'):
            code='PREP-ROLLBACK-20261112-'+suffix
            assert not frappe.db.exists('Item',code)
            doc=frappe.get_doc(dict(doctype='Item',item_code=code,item_name=code,item_group='Products',stock_uom='Kg',is_stock_item=1,is_purchase_item=1,is_sales_item=0))
            doc.insert(ignore_permissions=True);items.append(code)
        raw=items[:3];salad=items[3];cups=items[4]
        before_default=frappe.db.get_value('Item',salad,'default_bom')
        manager.create_prepared_recipe(salad)
        manager.create_prepared_recipe(cups)
        assert manager.recipe(salad)['kind']=='prepared'
        def qty(code):return float(frappe.db.get_value('Bin',{'item_code':code,'warehouse':warehouse},'actual_qty') or 0)
        company=frappe.db.get_value('Warehouse',warehouse,'company')
        receipt=inventory._new_stock_entry('Material Receipt',company,warehouse,'Rollback preparation check')
        for code in raw:receipt.append('items',dict(item_code=code,qty=2,uom='Kg',stock_uom='Kg',conversion_factor=1,t_warehouse=warehouse,basic_rate=200,cost_center=branch.cost_center('Kurunegala')))
        inventory._submit(receipt)
        recipe=manager.save_recipe(salad,[dict(item=c,qty=q) for c,q in zip(raw,[600,250,150])],output_qty=1000,recipe_note='Rollback sample')
        assert [r['qty'] for r in recipe['ingredients']]==[600,250,150]
        assert recipe['output_qty']==1000
        active=manager.activate_recipe(salad,recipe['version'])
        assert frappe.db.get_value('Item',salad,'default_bom')==before_default
        request=dict(item_code=salad,version=active['version'],request_id='native-prep-retry-20261112',planned_qty=2000,actual_qty=1900,comment='Measured trimming yield')
        first=manager.complete_batch(**request)
        assert abs(qty(salad)-1.9)<1e-6
        for code,remaining in zip(raw,[.8,1.5,1.7]):assert abs(qty(code)-remaining)<1e-6,(code,qty(code))
        assert first['received_qty']==1.9 and len(first['ingredients'])==3
        assert manager.complete_batch(**request)['already_processed']
        blocked(lambda:manager.complete_batch(**dict(request,actual_qty=1800)),'different values')
        blocked(lambda:manager.complete_batch(**dict(request,request_id='short-raw-stock',planned_qty=100000,actual_qty=100000)),'Insufficient stock')
        assert abs(qty(salad)-1.9)<1e-6
        batch=manager.recipe(salad)['batches'][0]
        changed=manager.save_recipe(salad,[dict(item=raw[0],qty=1000)],expected_version=active['version'],output_qty=900,recipe_note='New draft only')
        old=manager.recipe(salad,version=active['version'])
        assert old['output_qty']==1000 and old['ingredients'][0]['qty']==600
        assert manager.recipe(salad)['batches'][0]==batch
        cup=manager.save_recipe(cups,[dict(item=salad,qty=100)],output_qty=100)
        manager.activate_recipe(cups,cup['version'])
        vegetable_balances=[qty(code) for code in raw]
        manager.complete_batch(cups,cup['version'],'prepared-component-check',100,100)
        assert abs(qty(salad)-1.8)<1e-6
        assert [qty(code) for code in raw]==vegetable_balances
        _,components=inventory._components(cups,1,company,recipe_bom=cup['version'],resolve_default=False)
        assert len(components)==1 and components[0]['item_code']==salad
        blocked(lambda:manager.save_recipe(salad,[dict(item=cups,qty=100)],expected_version=changed['version'],output_qty=100),'itself')
        blocked(lambda:manager.create_prepared_recipe(salad,restaurant='Other'),'Switch')
        frappe.set_user('Guest');blocked(lambda:manager.complete_batch(**request),'manager');frappe.set_user('Administrator')
        print('PASS native batch yield, immediate stock consumption, receipt/retry guards, shortages, prepared component, history, branch and manager permissions')
finally:frappe.db.rollback();frappe.destroy()
