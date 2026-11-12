"""Real ERPNext integration checks; every sample and list move is rolled back."""
import json
import sys
from unittest.mock import patch
sys.path.insert(0, '/tmp/luuv-equipment-inventory-20261112/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-equipment-inventory-20261112/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager, inventory, inventory_sections as sections, inventory_purchasing, purchases
    from erpnext.stock.utils import get_stock_balance
    warehouse=branch.warehouse('Kurunegala');item='Water jug'
    def rejected(fn,text):
        save='p'+frappe.generate_hash(length=8);frappe.db.savepoint(save)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as exc:
            assert text.lower() in str(exc).lower(),str(exc)
            frappe.db.rollback(save_point=save);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        original=sections.settings(warehouse)
        assert item not in original['items']
        before_item=frappe.get_doc('Item',item).as_dict()
        before_bin=frappe.get_doc('Bin',frappe.db.get_value('Bin',{'item_code':item,'warehouse':warehouse},'name')).as_dict()
        before_sle=frappe.db.count('Stock Ledger Entry',{'item_code':item,'warehouse':warehouse})
        before_bills=frappe.get_all('Purchase Bill Item',filters={'item':item},fields=['name','category','qty','price','asset'])
        manager.set_minimum(item,10,'Kurunegala')
        assert item in {r['item'] for r in inventory_purchasing.restock('Kurunegala')['items']}
        move=dict(item_code=item,section='main',expected_revision=original['revision'],category='Kitchen Equipment',comment='Rollback-only equipment review',restaurant='Kurunegala')
        manager.move_inventory_item(**move)
        data=manager.dashboard('Kurunegala')
        assert item not in {r['item'] for r in data['stock']}
        row=next(r for r in data['main_inventory'] if r['item']==item and r['source']=='stock')
        assert row['balance']==before_bin['actual_qty']
        assert item not in {r['item'] for r in inventory_purchasing.restock('Kurunegala')['items']}
        assert item not in {r['item'] for r in manager._catalog(warehouse)}
        assert item not in {r['item'] for r in inventory._recipe(item,'Kurunegala')['catalog']}
        assert item not in {r['item'] for r in inventory.get_purchase_recipe_items(restaurant='Kurunegala')['items']}
        assert frappe.get_doc('Item',item).as_dict()==before_item
        assert frappe.get_doc('Bin',before_bin['name']).as_dict()==before_bin
        assert frappe.db.count('Stock Ledger Entry',{'item_code':item,'warehouse':warehouse})==before_sle
        assert frappe.get_all('Purchase Bill Item',filters={'item':item},fields=['name','category','qty','price','asset'])==before_bills
        assert all(frappe.db.get_value('Asset',r['asset'],'location')=='Kurunegala Restaurant' for r in data['main_inventory'] if r['source']=='asset')
        assert not sections.main_items(branch.warehouse('Luuv Fryxo')) or item not in sections.main_items(branch.warehouse('Luuv Fryxo'))
        rejected(lambda:manager.move_inventory_item(**move),'sections changed')
        rejected(lambda:inventory._save_recipe(item,[{'item':'Sun flower oil','qty':1}],'Kurunegala'),'equipment')
        rejected(lambda:inventory._save_recipe('Sun flower oil',[{'item':item,'qty':1}],'Kurunegala'),'equipment')
        print('PASS main/running separation, recipes and restock filtering, existing asset visibility and exact stock/history preservation',flush=True)
        # Future receipts add quantity to the same equipment record, without returning it to running stock.
        old=frappe.local.form_dict
        frappe.local.form_dict=frappe._dict(vendor='LFEQUIP-rollback',lines=json.dumps([dict(item=item,qty=1,price=530,unit='Nos',category='Food / Ingredients')]))
        bill=purchases.create_purchase_bill()['name'];frappe.local.form_dict=old
        purchases._decide_purchase_bill(bill,'approved')
        fresh=manager.dashboard('Kurunegala')
        assert next(r for r in fresh['main_inventory'] if r['item']==item and r['source']=='stock')['balance']==row['balance']+1
        assert item not in {r['item'] for r in fresh['stock']}
        manager.move_inventory_item(item,'running',fresh['section_revision'],comment='Rollback-only return',restaurant='Kurunegala')
        assert item in {r['item'] for r in manager.dashboard('Kurunegala')['stock']}
        assert float(get_stock_balance(item,warehouse))==row['balance']+1
        current=sections.settings(warehouse)
        with patch.object(manager,'_menu',return_value=[item]):
            rejected(lambda:manager.move_inventory_item(**dict(move,expected_revision=current['revision'])),'menu or recipe')
        rejected(lambda:manager.move_inventory_item(**dict(move,expected_revision=current['revision'],restaurant='Luuv Fryxo')),'Switch')
        frappe.set_user('Guest');rejected(lambda:manager.move_inventory_item(**move),'manager');frappe.set_user('Administrator')
        print('PASS future receipts stay in Main inventory, reversible moves, stale edits, menu and access guards',flush=True)
    frappe.db.rollback()
    assert sections.settings(warehouse)==original
    assert not frappe.db.exists('Purchase Bill',{'vendor':'LFEQUIP-rollback'})
    assert frappe.db.count('Stock Ledger Entry',{'item_code':item,'warehouse':warehouse})==before_sle
    print('PASS all equipment test changes rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
