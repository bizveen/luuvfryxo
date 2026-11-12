"""Exercise approved category corrections with real ERPNext; roll back all fixtures."""
import json
import math
import sys
from unittest.mock import patch
sys.path.insert(0, '/tmp/luuv-approved-categories-20261112/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-approved-categories-20261112/stage/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch, activity, roles
    from luuvcrm.api import purchases, manager_inventory as manager, purchase_categories as api, categories
    from erpnext.stock.utils import get_stock_balance
    token='LFCOR-'+frappe.generate_hash(length=8)
    def call(fn, **data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    def rejected(fn, message):
        point='p'+frappe.generate_hash(length=8);frappe.db.savepoint(point)
        try:fn()
        except (frappe.ValidationError, frappe.PermissionError) as exc:
            assert message.lower() in str(exc).lower(),str(exc)
            frappe.db.rollback(save_point=point);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+message)
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'), \
         patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        raw=manager.create_ingredient(token,'Kg',restaurant='Kurunegala')['item']
        warehouse=frappe.db.get_value('Restaurant','Kurunegala','warehouse')
        def balance():return float(get_stock_balance(raw,warehouse) or 0)
        def create():
            return call(purchases.create_purchase_bill,vendor=token,lines=json.dumps([
                dict(item=raw,qty=2,price=100,unit='Kg',category='Food / Ingredients'),
                dict(item=raw,qty=3,price=200,unit='Kg',category='Food / Ingredients')]))['name']
        bill=create();purchases._decide_purchase_bill(bill,'approved',discount=50)
        initial=api.review(bill);rows=initial['bill']['lines'];original=frappe.get_doc('Purchase Bill',bill)
        receipt=original.stock_entry;original_receipt=frappe.get_doc('Stock Entry',receipt).as_dict()
        original_fields={k:original.get(k) for k in ['vendor','purchase_date','total','discount','resolved_at','resolved_by','stock_entry','payment_status']}
        old_item_prices=frappe.get_all('Item Price',filters={'item_code':raw},fields=['name','price_list_rate','modified'])
        nonstock=next(c['value'] for c in initial['categories'] if not c['in_stock'])
        request=dict(name=bill,line_categories=[dict(row_name=rows[0]['row_name'],category=nonstock)],
                     expected_revision=initial['revision'],request_id=frappe.generate_hash(length=32),comment='Correct first row only')
        result=api.correct(**request)
        assert balance()==3
        assert result['bill']['total']==750 and result['bill']['discount']==50
        assert [r['category'] for r in result['bill']['lines']]==[nonstock,'Food / Ingredients']
        assert [(r['item'],r['qty'],r['unit'],r['price']) for r in result['bill']['lines']]==[(r['item'],r['qty'],r['unit'],r['price']) for r in rows]
        assert frappe.get_doc('Stock Entry',receipt).as_dict()==original_receipt
        assert {k:frappe.get_doc('Purchase Bill',bill).get(k) for k in original_fields}==original_fields
        assert frappe.get_all('Item Price',filters={'item_code':raw},fields=['name','price_list_rate','modified'])==old_item_prices
        assert api.correct(**request)['already_processed'] and balance()==3
        rejected(lambda:api.correct(**dict(request,comment='changed')), 'already used')
        rejected(lambda:api.correct(**dict(request,request_id=frappe.generate_hash(length=32))), 'another screen')
        spend=categories.get_category_bills(nonstock,days=7)
        current_bill=next(b for b in spend['rows'] if b['name']==bill)
        assert math.isclose(current_bill['category_total'],187.5,abs_tol=1e-6)
        print('PASS removal, independent duplicate rows, total/discount/unit/price/approval/receipt preservation, report bucket and retry/stale guards',flush=True)
        # Opposite corrections for the same Item must preserve both stock costs.
        swap=dict(name=bill,line_categories=[
            dict(row_name=rows[0]['row_name'],category='Food / Ingredients'),
            dict(row_name=rows[1]['row_name'],category=nonstock)],
            expected_revision=result['revision'],request_id=frappe.generate_hash(length=32))
        submit=api.inventory._submit
        def fail_receipt(entry, **kwargs):
            if entry.purpose=='Material Receipt':frappe.throw('Injected receipt failure')
            return submit(entry, **kwargs)
        with patch.object(api.inventory,'_submit',side_effect=fail_receipt):
            rejected(lambda:api.correct(**swap),'Injected receipt failure')
        assert balance()==3 and len(api.review(bill)['history'])==1
        frappe.db.savepoint('mixed_correction')
        mixed=api.correct(**swap)
        assert balance()==2
        entries=[frappe.get_doc('Stock Entry',n) for n in mixed['history'][0]['new_stock_entries']]
        assert [(e.purpose,float(e.items[0].qty)) for e in entries]==[('Material Issue',3),('Material Receipt',2)]
        assert float(entries[1].items[0].basic_rate)==100
        frappe.db.rollback(save_point='mixed_correction')
        assert balance()==3
        print('PASS mixed issue/receipt costs and complete rollback after a partial stock-entry failure',flush=True)
        revert=api.correct(bill,[dict(row_name=rows[0]['row_name'],category='Food / Ingredients')],
            result['revision'],frappe.generate_hash(length=32),'Restore ingredient classification')
        assert balance()==5 and len(revert['history'])==2
        assert revert['history'][0]['movements'][0]['delta']==2
        # Not enough remaining stock: category and ledger must both stay unchanged.
        manager.correct_stock(raw,1,5,'Rollback-only shortage fixture',restaurant='Kurunegala')
        rejected(lambda:api.correct(bill,[dict(row_name=rows[0]['row_name'],category=nonstock)],
            revert['revision'],frappe.generate_hash(length=32)), 'Not enough stock')
        assert balance()==1 and api.review(bill)['bill']['lines'][0]['category']=='Food / Ingredients'
        rejected(lambda:api.correct(bill,[dict(row_name='foreign-row',category=nonstock)],
            revert['revision'],frappe.generate_hash(length=32)), 'another bill')
        rejected(lambda:api.correct(bill,[dict(row_name=rows[0]['row_name'],category='_missing')],
            revert['revision'],frappe.generate_hash(length=32)), 'valid category')
        print('PASS restoration, append-only history and atomic shortage/invalid-input rejection',flush=True)
        legacy=create();frappe.db.set_value('Purchase Bill',legacy,'status','approved')
        old=api.review(legacy);before=balance()
        fixed=api.correct(legacy,[dict(row_name=old['bill']['lines'][0]['row_name'],category=nonstock)],
            old['revision'],frappe.generate_hash(length=32))
        assert balance()==before and not fixed['history'][0]['new_stock_entries']
        pending=create();rejected(lambda:api.review(pending),'Only approved')
        with patch.object(branch,'require_access',side_effect=frappe.PermissionError('Other branch')):
            rejected(lambda:api.review(bill),'Other branch')
        frappe.set_user('Guest');rejected(lambda:api.review(bill),'manager');frappe.set_user('Administrator')
        print('PASS legacy bills without receipts, pending bills, guest and branch isolation',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',raw)
    assert not frappe.db.exists('Purchase Bill',{'vendor':token})
    assert not frappe.db.exists('Stock Ledger Entry',{'item_code':raw})
    print('PASS all sample bills, stock movements and correction history rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
