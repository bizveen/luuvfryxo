import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from server import Store


class ApprovedCategoriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'review.sqlite3');self.store.dispatch('demo_stock',{})
        self.item='KG-STOCK-HOT'
        self.name=self.call('create_purchase_bill',_actor='Local purchaser',vendor='Approved sample',lines=json.dumps([
            dict(item=self.item,qty=2,price=100,unit='L',category='Food / Ingredients'),
            dict(item=self.item,qty=3,price=200,unit='L',category='Food / Ingredients')]))['name']
        self.call('resolve_approval',name=self.name,decision='approved')
    def call(self,method,**args):
        args.setdefault('_actor','Local manager');return self.store.dispatch('native:'+method,args)
    def review(self):return self.call('purchase_categories.review',name=self.name)
    def balance(self):return next(r['balance'] for r in self.store.dispatch('dashboard',{})['stock'] if r['item']==self.item)
    def request(self,data,category):
        return dict(name=self.name,expected_revision=data['revision'],request_id=uuid4().hex,
                    line_categories=json.dumps([dict(row_name=data['bill']['lines'][0]['row_name'],category=category)]))
    def test_reverse_restore_and_retry_preserve_bill_and_original_receipt(self):
        original=self.review();before=self.balance();request=self.request(original,'Staff Meals')
        result=self.call('purchase_categories.correct',**request)
        self.assertEqual(self.balance(),before-2)
        self.assertEqual(result['bill']['total'],original['bill']['total'])
        self.assertEqual(result['bill']['stock_entry'],original['bill']['stock_entry'])
        self.assertEqual(result['bill']['lines'][0]['qty'],2)
        self.assertEqual(result['bill']['lines'][1]['category'],'Food / Ingredients')
        self.assertTrue(self.call('purchase_categories.correct',**request)['already_processed'])
        self.assertEqual(self.balance(),before-2)
        restored=self.call('purchase_categories.correct',**self.request(result,'Food / Ingredients'))
        self.assertEqual(self.balance(),before)
        self.assertEqual(len(restored['history']),2)
        with self.store.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                db.execute('DELETE FROM purchase_category_corrections')
    def test_short_stock_stale_and_wrong_row_do_not_partially_apply(self):
        original=self.review();req=self.request(original,'Staff Meals')
        with self.store.connect() as db:db.execute('UPDATE stock SET balance=1 WHERE item=?',(self.item,))
        with self.assertRaisesRegex(ValueError,'Not enough stock'):self.call('purchase_categories.correct',**req)
        self.assertEqual(self.review()['bill']['lines'][0]['category'],'Food / Ingredients')
        self.assertEqual(len(self.review()['history']),0)
        self.assertEqual(self.balance(),1)
        with self.store.connect() as db:db.execute('UPDATE stock SET balance=5 WHERE item=?',(self.item,))
        self.call('purchase_categories.correct',**req)
        with self.assertRaisesRegex(ValueError,'another screen'):
            self.call('purchase_categories.correct',**dict(req,request_id=uuid4().hex))
        current=self.review();bad=self.request(current,'Food / Ingredients')
        bad['line_categories']=json.dumps([dict(row_name='other-bill-row',category='Staff Meals')])
        with self.assertRaisesRegex(ValueError,'another bill'):self.call('purchase_categories.correct',**bad)
    def test_purchaser_cannot_correct_approved_bill(self):
        with self.assertRaisesRegex(ValueError,'manager'):
            self.call('purchase_categories.review',name=self.name,_actor='Local purchaser')


if __name__=='__main__':unittest.main()
