import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from server import Store


class ExistingPurchasesTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'review.sqlite3')
        self.store.dispatch('demo_stock',{})
        self.item='KG-STOCK-HOT'

    def call(self,method,**p): return self.store.dispatch('native:'+method,p)
    def stock(self): return {r['item']:r for r in self.store.dispatch('dashboard',{})['stock']}
    def bill(self,qty=2,category='Food / Ingredients',actor='Local purchaser',item=None,unit='L'):
        return self.call('create_purchase_bill',vendor='Test Central Kitchen',_actor=actor,
            lines=json.dumps([dict(item=item or self.item,qty=qty,price=1500,unit=unit,category=category)]))['name']
    def resolve(self,name,decision='approved'):
        return self.call('resolve_approval',name=name,decision=decision,_actor='Local manager')

    def test_pending_bill_then_manager_approval_uses_one_receipt(self):
        before=self.stock()[self.item]
        name=self.bill()
        self.assertEqual(self.stock()[self.item]['balance'],before['balance'])
        self.assertEqual(self.stock()[self.item]['pending_qty'],2)
        receipt=self.resolve(name)
        after=self.stock()[self.item]
        self.assertEqual(after['balance'],before['balance']+2)
        self.assertAlmostEqual(after['rate'],(before['balance']*before['rate']+3000)/(before['balance']+2))
        self.assertEqual(after['pending_qty'],0)
        self.assertTrue(self.resolve(name)['already_processed'])
        self.assertEqual(self.stock()[self.item],after)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM movements WHERE kind='purchase_bill'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT stock_entry FROM purchase_bills WHERE name=?',(name,)).fetchone()[0],receipt['stock_entry'])

    def test_reject_and_nonstock_category_never_receive(self):
        before=self.stock()[self.item]['balance']
        self.resolve(self.bill(),'rejected')
        self.resolve(self.bill(category='Staff Meals'))
        self.assertEqual(self.stock()[self.item]['balance'],before)
        self.assertEqual(self.stock()[self.item]['pending_qty'],0)

    def test_permissions_branch_and_immutable_history(self):
        name=self.bill(actor='Local manager')
        with self.assertRaisesRegex(ValueError,'Someone else'):self.resolve(name)
        with self.assertRaisesRegex(ValueError,'Switch to Manager'):self.call('resolve_approval',name=name,decision='approved')
        with self.assertRaisesRegex(ValueError,'Switch to that branch'):self.store.dispatch('restock',{'restaurant':'Other branch'})
        with self.store.connect() as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):db.execute('UPDATE purchase_bills SET payload=? WHERE name=?',('{}',name))

    def test_concurrent_approval_and_opposite_decision(self):
        name=self.bill();before=self.stock()[self.item]['balance']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.resolve(name),range(2)))
        self.assertEqual(sum(bool(r.get('already_processed')) for r in results),1)
        self.assertEqual(self.stock()[self.item]['balance'],before+2)
        with self.assertRaisesRegex(ValueError,'already been resolved'):self.resolve(name,'rejected')

    def test_restock_pending_and_prepared_labels(self):
        self.store.dispatch('set_minimum',dict(item_code=self.item,minimum_qty=13))
        self.store.dispatch('set_minimum',dict(item_code='KG-STOCK-SALAD',minimum_qty=20))
        self.bill()
        data=self.store.dispatch('restock',{})
        row=next(r for r in data['items'] if r['item']==self.item)
        self.assertEqual(row['suggested_qty'],1)
        self.assertEqual(row['pending_qty'],2)
        self.assertTrue(next(r for r in data['items'] if r['item']=='KG-STOCK-SALAD')['prepared'])
        self.assertNotIn('Packaging',data['stock_categories'])
        self.resolve(self.bill(qty=1))
        row=next(r for r in self.store.dispatch('restock',{})['items'] if r['item']==self.item)
        self.assertEqual(row['suggested_qty'],0)

    def test_whole_nos_units_and_atomic_validation(self):
        box=self.store.dispatch('create_ingredient',dict(item_name='Bill test boxes',uom='nos'))['item']
        with self.assertRaisesRegex(ValueError,'whole nos'):self.bill(item=box,unit='nos',qty=1.5)
        with self.assertRaisesRegex(ValueError,'unit changed'):self.bill(unit='ml')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM purchase_bills').fetchone()[0],0)

    def test_approval_failure_rolls_back_and_can_retry(self):
        name=self.bill();before=self.stock()
        with self.store.connect() as db:db.execute("UPDATE stock SET stock_uom='ml' WHERE item=?",(self.item,))
        with self.assertRaisesRegex(ValueError,'unit changed'):self.resolve(name)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM purchase_bills WHERE name=?',(name,)).fetchone()[0],'pending')
            self.assertEqual(db.execute("SELECT count(*) FROM movements WHERE kind='purchase_bill'").fetchone()[0],0)
            db.execute("UPDATE stock SET stock_uom='L' WHERE item=?",(self.item,))
        self.resolve(name)
        self.assertEqual(self.stock()[self.item]['balance'],before[self.item]['balance']+2)

    def test_same_timestamp_still_guards_stale_recipe(self):
        old=self.store.dispatch('recipe',{})
        with patch('server.now',return_value=old['versions'][0]['creation']):
            self.store.dispatch('save_recipe',dict(ingredients=old['ingredients'],expected_version=old['version']))
            with self.assertRaisesRegex(ValueError,'another screen'):
                self.store.dispatch('save_recipe',dict(ingredients=old['ingredients'],expected_version=old['version']))

    def test_two_preview_tabs_keep_their_roles(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.parse import urlencode
        from urllib.request import Request,urlopen
        from server import handler
        app=ThreadingHTTPServer(('127.0.0.1',0),handler(self.store))
        thread=Thread(target=app.serve_forever,daemon=True);thread.start()
        try:
            base='http://127.0.0.1:'+str(app.server_port)
            for route in ('/manager','/manager2.0'):
                with self.subTest(route=route):
                    name=self.bill()
                    body=urlencode(dict(name=name,decision='approved')).encode()
                    request=Request(base+'/api/method/luuvcrm.api.resolve_approval',data=body,
                        headers={'Cookie':'preview_role=purchaser','Referer':base+route+'?tab=approvals'})
                    with urlopen(request) as response:result=json.load(response)
                    self.assertTrue(result['message']['ok'])
        finally:
            app.shutdown();app.server_close();thread.join()


if __name__=='__main__': unittest.main()
