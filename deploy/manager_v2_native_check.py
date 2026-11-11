"""Native ERPNext release test: unique fixtures, no commits, queues or live events."""
import json
import math
import sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-manager-v2-20261111/stage')
import frappe
import luuvcrm

assert '/tmp/luuv-manager-v2-20261111/stage/' in luuvcrm.__file__
frappe.init(site='fryxo')
frappe.connect()
frappe.set_user('Administrator')
raw=None
meal=None
try:
    from luuvcrm import branch, activity
    from luuvcrm.api import inventory, manager_inventory as manager, branch_stock_policy as rules, purchases
    from luuvcrm.api.inventory_purchasing import restock
    from erpnext.stock.utils import get_stock_balance
    warehouse=frappe.db.get_value('Restaurant','Kurunegala','warehouse')
    token='LFV2-'+frappe.generate_hash(length=8)
    with patch.object(frappe.db,'commit'), patch.object(frappe,'enqueue'), \
         patch.object(frappe,'publish_realtime'), patch.object(activity,'log_activity'), \
         patch.object(branch,'selected',return_value='Kurunegala'):
        dashboard=manager.dashboard('Kurunegala')
        assert dashboard['warehouse']==warehouse
        assert restock('Kurunegala')['warehouse']==warehouse
        print('PASS real branch dashboard and purchase stock queries',flush=True)
        try:manager.dashboard('Other branch')
        except frappe.PermissionError:pass
        else:raise AssertionError('Cross-branch access accepted')
        frappe.set_user('Guest')
        try:manager.dashboard('Kurunegala')
        except frappe.PermissionError:pass
        else:raise AssertionError('Guest access accepted')
        frappe.set_user('Administrator')

        raw=manager.create_ingredient(token+' ingredient','Kg',minimum_qty=1,restaurant='Kurunegala')['item']
        audit=manager.correct_stock(raw,.135,0,'Rollback release opening count',valuation_rate=100,restaurant='Kurunegala')
        def balance():return float(get_stock_balance(raw,warehouse) or 0)
        assert math.isclose(balance(),.135,abs_tol=1e-6)
        assert audit['details']['difference']==.135
        print('PASS native ingredient creation, MOQ and stock audit',flush=True)

        meal=token+'-MEAL'
        frappe.get_doc(dict(doctype='Item',item_code=meal,item_name=meal,item_group='Raw Materials',
            stock_uom='Nos',is_stock_item=0,is_sales_item=1)).insert(ignore_permissions=True)
        with patch.object(manager,'_menu',return_value=[meal]):
            policy=rules.current('Kurunegala')
            policy['items'][raw]=dict(mode='percent',percent=10)
            rules.save(policy,'Kurunegala')
            first=manager.save_recipe(meal,[dict(item=raw,qty=150)],restaurant='Kurunegala')
            active=manager.activate_recipe(meal,first['version'],'Kurunegala')
            assert active['status']=='Active'
            assert inventory._default_bom(meal) is None
            def order():
                doc=frappe.get_doc(dict(doctype='POS Order',restaurant='Kurunegala',order_type='Take Away',
                    items=[dict(item=meal,qty=1,rate=100,amount=100)]))
                return doc.insert(ignore_permissions=True,ignore_mandatory=True).name
            first_order=order()
            issue=inventory.consume_order_stock(first_order)
            assert math.isclose(balance(),-.015,abs_tol=1e-6)
            assert issue['stock_warnings'][0]['percent']==10
            assert inventory.consume_order_stock(first_order)['consumed']==0
            waiting=order()
            try:inventory.consume_order_stock(waiting)
            except frappe.ValidationError:pass
            else:raise AssertionError('Tolerance overspend was accepted')
            assert math.isclose(balance(),-.015,abs_tol=1e-6)
            print('PASS native 10% allowance, negative ledger, blocked overspend and duplicate send',flush=True)

            policy=rules.current('Kurunegala');policy['items'][raw]=dict(mode='negative',percent=0)
            rules.save(policy,'Kurunegala')
            second=manager.save_recipe(meal,[dict(item=raw,qty=200)],expected_version=first['version'],restaurant='Kurunegala')
            manager.activate_recipe(meal,second['version'],'Kurunegala')
            inventory.consume_order_stock(waiting)
            assert math.isclose(balance(),-.165,abs_tol=1e-6)
            inventory.consume_order_stock(order())
            assert math.isclose(balance(),-.365,abs_tol=1e-6)
            old=json.loads(frappe.db.get_value('Stock Entry',issue['entry'],'luuv_stock_policy_snapshot'))
            assert old['warnings'][0]['percent']==10
            assert manager.recipe(meal,'Kurunegala',first['version'])['ingredients'][0]['qty']==150
            print('PASS new recipe versions preserve waiting orders and posted stock history',flush=True)

        bill=frappe.get_doc(dict(doctype='Purchase Bill',restaurant='Kurunegala',vendor=token,
            status='pending',purchase_date=frappe.utils.today(),requested_by='Administrator',
            items=[dict(item=raw,qty=.1,price=100,uom='Kg',category='Food / Ingredients')]))
        bill.insert(ignore_permissions=True,ignore_mandatory=True)
        purchases._decide_purchase_bill(bill.name,'approved')
        assert math.isclose(balance(),-.265,abs_tol=1e-6)
        assert purchases._decide_purchase_bill(bill.name,'approved')==0
        assert math.isclose(balance(),-.265,abs_tol=1e-6)
        manager.correct_stock(raw,0,-.265,'Rollback physical count after shortage',restaurant='Kurunegala')
        assert math.isclose(balance(),0,abs_tol=1e-6)
        assert frappe.db.get_single_value('Stock Settings','allow_negative_stock')==0
        print('PASS actual purchase approval, partial negative receipt, retry and count correction',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',raw)
    assert not frappe.db.exists('Item',meal)
    assert not frappe.db.exists('BOM',{'item':meal})
    assert not frappe.db.exists('Stock Ledger Entry',{'item_code':raw})
    print('PASS all release fixtures rolled back; no production stock, recipe or policy changes',flush=True)
finally:
    frappe.db.rollback()
    frappe.destroy()
