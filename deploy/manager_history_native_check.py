"""Native verification; all temporary purchases, defaults and approvals roll back."""
import json
import sys
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-manager-history-20261112/stage')
import frappe
import luuvcrm
assert '/tmp/luuv-manager-history-20261112/stage/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import purchases,purchase_categories,manager_inventory,purchase_item_categories as defaults,removal_report,approvals
    token='LFHISTORY-'+frappe.generate_hash(length=8)
    def call(fn,**data):
        old=frappe.local.form_dict;frappe.local.form_dict=frappe._dict(data)
        try:return fn()
        finally:frappe.local.form_dict=old
    def reject(fn,text):
        point='p'+frappe.generate_hash(length=8);frappe.db.savepoint(point)
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as error:
            assert text.lower() in str(error).lower(),str(error)
            frappe.db.rollback(save_point=point);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    with patch.object(frappe.db,'commit'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'), \
         patch.object(activity,'log_activity'),patch.object(branch,'selected',return_value='Kurunegala'):
        item=manager_inventory.create_ingredient(token,'Kg',restaurant='Kurunegala')['item']
        def create(category='Food / Ingredients',v2=False):
            return call(purchases.create_purchase_bill,vendor=token,use_item_categories=int(v2),lines=json.dumps([
                dict(item=item,name=token,qty=2,price=100,unit='Kg',category=category)]))['name']
        bill=create();old=create();purchases._decide_purchase_bill(bill,'approved')
        original=frappe.get_doc('Purchase Bill',old).as_dict()
        current=purchase_categories.review(bill)
        result=purchase_categories.correct(name=bill,expected_revision=current['revision'],request_id=frappe.generate_hash(length=32),
            line_categories=[dict(row_name=current['bill']['lines'][0]['row_name'],category='Staff Meals')])
        assert defaults.category(defaults.read(),item,token)=='Staff Meals'
        assert next(r for r in defaults.catalog()['items'] if r['item']==item)['default_category']=='Staff Meals'
        reject(lambda:create(v2=True),'now uses')
        future=create('Staff Meals',True);assert purchases._bill_dict(future)['lines'][0]['category']=='Staff Meals'
        assert frappe.get_doc('Purchase Bill',old).as_dict()==original
        # Older clients retain their original bill workflow.
        legacy=create();assert purchases._bill_dict(legacy)['lines'][0]['category']=='Food / Ingredients'
        doc=frappe.get_doc('Purchase Bill',future)
        purchases._apply_purchase_line_categories(doc,[dict(row_name=doc.items[0].name,category='Not sure')])
        assert defaults.category(defaults.read(),item,token)=='Not sure'
        # Exact descriptive name can use the same saved choice without an Item code.
        desc=call(purchases.create_purchase_bill,vendor=token,use_item_categories=1,lines=json.dumps([
            dict(name=token,qty=1,price=200,unit='Bill',category='Not sure')]))['name']
        assert purchases._bill_dict(desc)['lines'][0]['category']=='Not sure'
        other=frappe.get_all('Restaurant',filters={'name':['!=','Kurunegala']},pluck='name',limit_page_length=1)[0]
        assert defaults.category(defaults.read(other),item,token) is None
        print('PASS saved choices across future bills, descriptive names, stale-category rejection, old bill preservation and branch isolation',flush=True)
        # A removal is identified from the saved before/after quantities, not the live order.
        order=frappe.db.get_value('POS Order',{branch.FIELD:'Kurunegala'},'name')
        assert order
        fixture_names=[]
        for status,action,removed in [('approved','update_items',True),('rejected','update_items',True),('pending','update_items',False),('approved','discount',False)]:
            row=frappe.get_doc(dict(doctype='POS Approval',approval_type='edit',pos_order=order,
                title=token,action=action,status='pending',requested_by='Guest',requested_at=frappe.utils.now_datetime(),
                restaurant='Kurunegala',details_json=json.dumps({'lines':[{'name':'Sample fries','qtyStr':'1','totalStr':'500'}]}),
                action_payload=json.dumps({'_approval_base':{'items':[['sample',2,500,0,'']]},'items':[{'item':'sample','qty':1 if removed else 2}]}))).insert(ignore_permissions=True)
            row.db_set({'status':status,'resolved_by':'Administrator' if status!='pending' else None,
                        'resolved_at':frappe.utils.now_datetime() if status!='pending' else None})
            fixture_names.append(row.name)
        removal=removal_report.history();got={r['name']:r for r in removal['rows']}
        assert set(fixture_names[:2])<=set(got) and not set(fixture_names[2:])&set(got)
        assert got[fixture_names[0]]['by']=='Guest' and got[fixture_names[0]]['resolved_by']=='Administrator'
        assert got[fixture_names[0]]['created_when'] and got[fixture_names[0]]['resolved_when']
        approved=removal_report.history(status='approved');assert all(r['status']=='approved' for r in approved['rows'])
        bill_history=removal_report.history(kind='bill_changes');assert set(fixture_names)<=set(r['name'] for r in bill_history['rows'])
        all_history=removal_report.history(kind='all');assert set(fixture_names)<=set(r['name'] for r in all_history['rows'])
        with patch.object(branch,'selected',return_value=other):
            assert not set(fixture_names)&set(r['name'] for r in removal_report.history(kind='all')['rows'])
        reject(lambda:removal_report.history(start='2020-01-01'),'one year')
        frappe.set_user('Guest');reject(lambda:removal_report.history(),'manager');frappe.set_user('Administrator')
        print('PASS removal-only vs all bill changes, creator/approver timestamps, status/date filters, branch scope and manager access',flush=True)
    frappe.db.rollback()
    assert not frappe.db.exists('Item',item)
    assert not frappe.db.exists('POS Approval',{'title':token})
    assert defaults.category(defaults.read('Kurunegala'),item,token) is None
    print('PASS all native test changes rolled back',flush=True)
finally:frappe.db.rollback();frappe.destroy()
