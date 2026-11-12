"""Rollback-only verification of history markers with native purchase/report APIs."""
import json
import sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-category-highlights-20261112/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-category-highlights-20261112/stage/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import purchases,manager_inventory as manager,purchase_categories as api,categories
    token='LFHILITE-'+frappe.generate_hash(length=8)
    def call(fn,**data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'), \
         patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        item=manager.create_ingredient(token,'Kg',restaurant='Kurunegala')['item']
        def create():
            return call(purchases.create_purchase_bill,vendor=token,lines=json.dumps([
                dict(item=item,qty=2,price=100,unit='Kg',category='Food / Ingredients')]))['name']
        bill=create();untouched=create()
        doc=frappe.get_doc('Purchase Bill',bill)
        assert not purchases._bill_dict(bill)['category_changed']
        # No-op category submissions must not produce a marker or Version.
        purchases._apply_purchase_line_categories(doc,[dict(row_name=doc.items[0].name,category='Food / Ingredients')])
        assert not purchases._bill_dict(bill)['category_changed']
        purchases._apply_purchase_line_categories(doc,[dict(row_name=doc.items[0].name,category='Staff Meals')])
        current=purchases._bill_dict(bill)
        assert current['category_changed'] and current['category_change']['by']=='Administrator'
        assert current['category_change']['changes'][0]['after']=='Staff Meals'
        assert current['total']==200
        # Historical approvals before the new Version marker use their saved receipt.
        point='legacy_history';frappe.db.savepoint(point)
        frappe.db.delete('Version',{'ref_doctype':'Purchase Bill','docname':bill})
        legacy=purchases._bill_dict(bill)
        assert legacy['category_changed'] and not legacy['category_change']['by']
        frappe.db.rollback(save_point=point)
        purchases._decide_purchase_bill(bill,'approved')
        initial=api.review(bill)
        result=api.correct(name=bill,expected_revision=initial['revision'],request_id=frappe.generate_hash(length=32),
            line_categories=[dict(row_name=doc.items[0].name,category='Food / Ingredients')])
        assert result['bill']['category_changed']
        assert result['bill']['category_change']['changes'][0]['after']=='Food / Ingredients'
        assert not purchases._bill_dict(untouched)['category_changed']
        for rows in (purchases.get_purchase_bills()['bills'],
                     purchases.get_purchase_day_summary()['bills'],
                     categories.get_category_bills('Food / Ingredients',days=7)['rows']):
            assert next(b for b in rows if b['name']==bill)['category_changed']
        print('PASS no-op/unchanged bills, pending and approved changes, legacy receipt fallback, change-back history, day and report lists',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',item)
    assert not frappe.db.exists('Purchase Bill',{'vendor':token})
    print('PASS native sample changes rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
