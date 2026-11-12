"""Rollback-only native checks for unit edits and immutable stock/bill/recipe history."""
import json,sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-inventory-units-20261112/stage')
import frappe,luuvcrm
assert '/tmp/luuv-inventory-units-20261112/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
token='LFUNIT-'+frappe.generate_hash(length=10)
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager,inventory_units as units,purchases
    def call(fn,**data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    def blocked(fn,text):
        point='check_'+frappe.generate_hash(length=8);frappe.db.savepoint(point)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as error:
            assert text.lower() in str(error).lower(),str(error)
            frappe.db.rollback(save_point=point);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        def new(suffix,uom='Nos'):return manager.create_ingredient(token+suffix,uom,restaurant='Kurunegala')['item']
        fresh=new('-fresh');original_name=token+'-fresh'
        info=manager.inventory_item_units(fresh,'Kurunegala');assert info['can_change'] and not info['reasons']
        request=dict(item_code=fresh,item_name=original_name,expected_name=original_name,restaurant='Kurunegala',unit='Kg',expected_unit='Nos')
        assert manager.rename_inventory_item(**request)['changed']
        assert not manager.rename_inventory_item(**request)['changed']
        doc=frappe.get_doc('Item',fresh);assert doc.stock_uom=='Kg' and len(doc.uoms)==1 and doc.uoms[0].uom=='Kg' and doc.uoms[0].conversion_factor==1
        assert not frappe.db.exists('Stock Ledger Entry',{'item_code':fresh})
        info=manager.inventory_item_units(fresh,'Kurunegala');assert info['history'][0]['before']=='Nos' and info['history'][0]['after']=='Kg' and info['history'][0]['by']
        assert len(info['history'])==1
        blocked(lambda:manager.rename_inventory_item(**dict(request,unit='g')),'Reopen')
        blocked(lambda:manager.rename_inventory_item(**dict(request,unit='not-a-defined-unit',expected_unit='Kg')),'defined unit')
        disabled=token+'-disabled'
        frappe.get_doc(dict(doctype='UOM',uom_name=disabled,enabled=0)).insert(ignore_permissions=True)
        assert disabled not in [r['name'] for r in units.defined_units()]
        blocked(lambda:manager.rename_inventory_item(**dict(request,unit=disabled,expected_unit='Kg')),'defined unit')
        blocked(lambda:manager.create_ingredient(token+' invalid',disabled),'defined unit')
        blocked(lambda:call(purchases.create_purchase_item,item_name=token+' invalid',unit=disabled,category='Food / Ingredients'),'defined unit')
        print('PASS defined units, fresh item update, native conversion row, stale edits, retry, audit trail and disabled-unit validation',flush=True)
        # A pending bill already establishes a unit snapshot, even before its stock arrives.
        used=new('-used','Kg')
        bill=call(purchases.create_purchase_bill,vendor=token,lines=json.dumps([dict(item=used,qty=2,price=120,unit='Kg',category='Food / Ingredients')]))['name']
        assert 'Purchase bills' in units.describe(used)['reasons']
        blocked(lambda:manager.rename_inventory_item(used,token+' changed',token+'-used','Kurunegala','g','Kg'),'locked')
        assert frappe.db.get_value('Item',used,'item_name')==token+'-used'
        purchases._decide_purchase_bill(bill,'approved')
        receipt=frappe.db.get_value('Purchase Bill',bill,'stock_entry')
        warehouse=branch.warehouse('Kurunegala')
        def snapshots():
            return dict(bill=frappe.get_all('Purchase Bill Item',filters={'parent':bill},fields=['*'],order_by='idx'),
                ledger=frappe.get_all('Stock Ledger Entry',filters={'item_code':used},fields=['*'],order_by='name'),
                receipt=frappe.get_all('Stock Entry Detail',filters={'parent':receipt},fields=['*'],order_by='idx'),
                bins=frappe.get_all('Bin',filters={'item_code':used},fields=['*'],order_by='name'))
        before=snapshots()
        blocked(lambda:call(purchases.rename_purchase_item,item=used,new_name=token+' changed',unit='g',expected_unit='Kg'),'locked')
        # The old purchases route also cannot re-label recorded stock.
        blocked(lambda:call(purchases.rename_purchase_item,item=used,new_name=token+' changed',unit='g'),'Reopen')
        manager.rename_inventory_item(used,token+' corrected',token+'-used','Kurunegala','Kg','Kg')
        assert snapshots()==before
        assert frappe.db.get_value('Bin',{'item_code':used,'warehouse':warehouse},'actual_qty')==2
        assert purchases._bill_dict(bill)['lines'][0]['unit']=='Kg' and purchases._bill_dict(bill)['lines'][0]['name']==token+'-used'
        assert units.describe(used)['can_change'] is False
        print('PASS pending/approved bill and stock locks; name-only edits still work; historical units, quantities and valuation unchanged',flush=True)
        recipe_item=new('-recipe','Kg');output=new('-output','Kg')
        bom=frappe.get_doc(dict(doctype='BOM',item=output,company=frappe.db.get_value('Warehouse',warehouse,'company'),quantity=1,is_active=1,is_default=0,luuv_recipe_restaurant='Kurunegala',items=[dict(item_code=recipe_item,qty=1,uom='Kg',conversion_factor=1)]));bom.insert(ignore_permissions=True)
        assert 'Recipe ingredients' in units.describe(recipe_item)['reasons']
        assert 'Prepared-item or menu recipes' in units.describe(output)['reasons']
        minimum=new('-minimum');manager.set_minimum(minimum,2,'Kurunegala');assert 'Minimum stock levels' in units.describe(minimum)['reasons']
        manager.set_minimum(minimum,0,'Kurunegala');assert units.describe(minimum)['can_change']
        priced=new('-price');frappe.get_doc(dict(doctype='Item Price',item_code=priced,price_list=purchases._buying_price_list(),buying=1,price_list_rate=25)).insert(ignore_permissions=True)
        assert 'Price book entries' in units.describe(priced)['reasons']
        # Purchase 2.0 shares the same guarded editor for genuinely unused items.
        purchase_new=new('-purchase')
        assert units.purchase_item(purchase_new)['can_change']
        call(purchases.rename_purchase_item,item=purchase_new,new_name=token+'-purchase',unit='L',expected_unit='Nos')
        assert frappe.db.get_value('Item',purchase_new,'stock_uom')=='L'
        blocked(lambda:manager.inventory_item_units(fresh,'Luuv Fryxo'),'Switch')
        frappe.set_user('Guest');blocked(lambda:manager.inventory_item_units(fresh),'manager');blocked(lambda:units.purchase_item(fresh),'purchasing');frappe.set_user('Administrator')
        print('PASS recipe/price/MOQ protection, purchase editor, branch and role gates',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',{'item_name':['like',token+'%']})
    assert not frappe.db.exists('Purchase Bill',{'vendor':token})
    print('PASS all native sample changes rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
