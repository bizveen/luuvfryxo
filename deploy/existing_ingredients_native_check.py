"""Native rollback-only checks: global Items can be reused by branch inventory."""
import json,sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-existing-ingredients-20261112/stage')
import frappe,luuvcrm
assert '/tmp/luuv-existing-ingredients-20261112/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
token='LFREUSE-'+frappe.generate_hash(length=10)
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager,purchases,inventory_sections
    warehouse=branch.warehouse('Kurunegala')
    def blocked(fn,text):
        point='reuse_'+frappe.generate_hash(length=8);frappe.db.savepoint(point)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as error:
            assert text.lower() in str(error).lower(),str(error)
            frappe.db.rollback(save_point=point);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    def call(fn,**data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'),patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        def item(suffix,**extra):
            doc=frappe.get_doc(dict(doctype='Item',item_code=token+suffix,item_name=token+suffix,
                item_group='Raw Materials',stock_uom='Kg',is_stock_item=1,is_purchase_item=1,is_sales_item=0))
            doc.update(extra);doc.insert(ignore_permissions=True);return doc
        hidden=item('-hidden')
        assert hidden.name not in [r['item'] for r in manager.dashboard()['stock']]
        assert hidden.name in [r['item'] for r in manager.ingredient_options()['items']]
        before_item=frappe.get_doc('Item',hidden.name).as_dict()
        result=manager.create_ingredient(' '+hidden.item_name.lower()+' ','Nos',999)
        assert result['existed'] and not result['already_registered'] and result['item']==hidden.name
        assert result['stock_uom']=='Kg' and result['minimum_qty']==0
        assert hidden.name in [r['item'] for r in manager.dashboard()['stock']]
        assert frappe.get_doc('Item',hidden.name).as_dict()==before_item
        assert not frappe.db.exists('Bin',{'item_code':hidden.name})
        manager.set_minimum(hidden.name,2)
        again=manager.create_ingredient(hidden.name,'L',999)
        assert again['already_registered'] and again['minimum_qty']==2 and again['stock_uom']=='Kg'
        assert frappe.db.count('Item',{'item_name':hidden.item_name})==1
        print('PASS hidden global Item registered; case/name/code/retry reuse preserves unit and MOQ',flush=True)
        bill=call(purchases.create_purchase_bill,vendor=token,lines=json.dumps([dict(item=hidden.name,qty=2,price=120,unit='Kg',category='Food / Ingredients')]))['name']
        purchases._decide_purchase_bill(bill,'approved')
        def snapshots():
            return dict(bill=frappe.get_all('Purchase Bill Item',filters={'parent':bill},fields=['*'],order_by='idx'),
                ledger=frappe.get_all('Stock Ledger Entry',filters={'item_code':hidden.name},fields=['*'],order_by='name'),
                bins=frappe.get_all('Bin',filters={'item_code':hidden.name},fields=['*'],order_by='name'),
                categories=frappe.db.get_value('Warehouse',warehouse,'luuv_purchase_categories'))
        before=snapshots();manager.create_ingredient(hidden.item_name,'Nos',500,item_code=hidden.name)
        assert snapshots()==before
        duplicate=item('-second',item_name=hidden.item_name)
        blocked(lambda:manager.create_ingredient(hidden.item_name,'Kg'),'item code')
        assert manager.create_ingredient(hidden.item_name,'L',item_code=duplicate.name)['item']==duplicate.name
        fresh=manager.create_ingredient(token+'-fresh','Kg',3);assert not fresh['existed']
        assert fresh['minimum_qty']==3
        print('PASS approved bill, ledger, balances and categories unchanged; ambiguous name selection and new creation',flush=True)
        disabled=item('-disabled',disabled=1)
        blocked(lambda:manager.create_ingredient(disabled.item_name,'Kg'),'disabled')
        nonstock=item('-nonstock',is_stock_item=0)
        blocked(lambda:manager.create_ingredient(nonstock.item_name,'Kg'),'without stock tracking')
        equipment=item('-equipment')
        manager.create_ingredient(equipment.item_name,'Kg')
        config=inventory_sections.settings(warehouse)
        inventory_sections.move(equipment.name,'main',config['revision'],'Other Equipment','Test equipment classification')
        blocked(lambda:manager.create_ingredient(equipment.item_name,'Kg'),'Main inventory')
        assert equipment.name not in [r['item'] for r in manager.ingredient_options()['items']]
        blocked(lambda:manager.create_ingredient('Missing','Kg',item_code=token+'-missing'),'no longer')
        blocked(lambda:manager.create_ingredient(token+' bad unit','invalid'),'defined unit')
        blocked(lambda:manager.ingredient_options('Luuv Fryxo'),'Switch')
        frappe.set_user('Guest');blocked(lambda:manager.ingredient_options(),'manager')
        blocked(lambda:manager.create_ingredient(hidden.item_name,'Kg'),'manager');frappe.set_user('Administrator')
        print('PASS disabled/nonstock/equipment protection, stale selection, branch and manager access',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',{'name':['like',token+'%']})
    assert not frappe.db.exists('Item',{'item_name':['like',token+'%']})
    assert not frappe.db.exists('Purchase Bill',{'vendor':token})
    print('PASS all temporary records rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
