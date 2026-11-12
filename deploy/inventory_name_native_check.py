"""Rollback-only integration tests for display names and immutable linked history."""
import json
import sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-inventory-name-20261112/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-inventory-name-20261112/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager,purchases
    token='LFNAME-'+frappe.generate_hash(length=8)
    def rejected(fn,text):
        save='p'+frappe.generate_hash(length=8);frappe.db.savepoint(save)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as exc:
            assert text.lower() in str(exc).lower(),str(exc)
            frappe.db.rollback(save_point=save);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        item=manager.create_ingredient(token,'Kg',restaurant='Kurunegala')['item']
        parent=manager.create_ingredient(token+' prepared','Kg',restaurant='Kurunegala')['item']
        old=frappe.local.form_dict
        frappe.local.form_dict=frappe._dict(vendor=token,lines=json.dumps([dict(item=item,qty=3,price=100,unit='Kg',category='Food / Ingredients')]))
        bill=purchases.create_purchase_bill()['name'];frappe.local.form_dict=old
        purchases._decide_purchase_bill(bill,'approved')
        warehouse=branch.warehouse('Kurunegala')
        bom=frappe.get_doc(dict(doctype='BOM',item=parent,company=frappe.db.get_value('Warehouse',warehouse,'company'),quantity=1,
            is_active=1,is_default=0,luuv_recipe_restaurant='Kurunegala',items=[dict(item_code=item,qty=1,uom='Kg',conversion_factor=1)]))
        bom.insert(ignore_permissions=True)
        bin_name=frappe.db.get_value('Bin',{'item_code':item,'warehouse':warehouse},'name')
        original=dict(bill=frappe.get_doc('Purchase Bill',bill).as_dict(),bom=frappe.get_doc('BOM',bom.name).as_dict(),
            bin=frappe.get_doc('Bin',bin_name).as_dict(),item=frappe.get_doc('Item',item).as_dict(),
            receipt=frappe.get_doc('Stock Entry',frappe.db.get_value('Purchase Bill',bill,'stock_entry')).as_dict())
        request=dict(item_code=item,item_name=token+' corrected',expected_name=token,restaurant='Kurunegala')
        result=manager.rename_inventory_item(**request)
        assert result['changed'] and result['item']==item
        current=frappe.get_doc('Item',item).as_dict()
        assert current['item_name']==request['item_name']
        for key in set(original['item'])-{'item_name','modified','modified_by'}:
            assert original['item'][key]==current[key],key
        assert original['bill']==frappe.get_doc('Purchase Bill',bill).as_dict()
        assert original['bom']==frappe.get_doc('BOM',bom.name).as_dict()
        assert original['bin']==frappe.get_doc('Bin',bin_name).as_dict()
        assert original['receipt']==frappe.get_doc('Stock Entry',original['receipt']['name']).as_dict()
        assert purchases._bill_dict(bill)['lines'][0]['name']==token
        assert next(r for r in manager.dashboard('Kurunegala')['stock'] if r['item']==item)['name']==request['item_name']
        assert next(r for r in purchases.get_purchase_catalog()['items'] if r['item']==item)['name']==request['item_name']
        assert manager.rename_inventory_item(**request)['changed'] is False
        count=frappe.db.count('Version',{'ref_doctype':'Item','docname':item,'data':['like','%inventory_name_change%']})
        assert count==1
        rejected(lambda:manager.rename_inventory_item(**dict(request,item_name=token+' stale')),'another screen')
        request['expected_name']=request['item_name']
        rejected(lambda:manager.rename_inventory_item(**dict(request,item_name=token+' prepared')),'already uses')
        for invalid in ('','x','<script>name</script>','new\nname','x'*141):
            rejected(lambda:manager.rename_inventory_item(**dict(request,item_name=invalid)),'plain-text')
        rejected(lambda:manager.rename_inventory_item(**dict(request,item_code='_other_branch_item')),'this branch')
        rejected(lambda:manager.rename_inventory_item(**dict(request,restaurant='Luuv Fryxo')),'Switch')
        print('PASS stable Item ID, stock/valuation/unit/recipe/bill/receipt preservation, future purchase label, retry and validation guards',flush=True)
        main=manager.dashboard('Kurunegala')['main_inventory']
        equipment=next(r for r in main if r['source']=='stock')
        asset=next(r for r in main if r['source']=='asset')
        asset_before=frappe.get_doc('Asset',asset['asset']).as_dict()
        for index,row in enumerate((equipment,asset)):
            manager.rename_inventory_item(row['item'],token+' equipment '+str(index),row['name'],'Kurunegala')
            assert next(r for r in manager.dashboard('Kurunegala')['main_inventory'] if r['item']==row['item'])['name']==token+' equipment '+str(index)
        assert frappe.get_doc('Asset',asset['asset']).as_dict()==asset_before
        frappe.set_user('Guest');rejected(lambda:manager.rename_inventory_item(**request),'manager');frappe.set_user('Administrator')
        print('PASS equipment and asset item labels, unchanged asset records and manager-only access',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',item)
    assert not frappe.db.exists('Purchase Bill',{'vendor':token})
    assert frappe.db.get_value('Item',asset['item'],'item_name')==asset['name']
    assert frappe.db.get_value('Item',equipment['item'],'item_name')==equipment['name']
    frappe.clear_document_cache('Item',asset['item']);frappe.clear_document_cache('Item',equipment['item'])
    print('PASS all sample changes rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
