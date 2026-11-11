import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
import test_inventory


class PurchasingAndOrderChangesTest(unittest.TestCase):
    setUp=test_inventory.InventoryFlowTest.setUp
    call=test_inventory.InventoryFlowTest.call
    recipe=test_inventory.InventoryFlowTest.recipe
    balances=test_inventory.InventoryFlowTest.balances
    activate=test_inventory.InventoryFlowTest.activate

    def purchase(self,**kwargs):
        params=dict(request_id='purchase-request-1',vendor='Test supplier',source='supplier',items=[dict(item='KG-STOCK-HOT',qty=2)])
        params.update(kwargs);return self.call('save_purchase',**params)

    def receive(self,order,**kwargs):
        params=dict(purchase=order['name'],request_id='receipt-request-1',bill_ref='TEST-001',items=[dict(item='KG-STOCK-HOT',qty=1,rate=400)])
        params.update(kwargs);return self.call('receive_purchase',**params)

    def test_pending_orders_do_not_add_stock_and_deduplicate(self):
        before=self.balances();order=self.purchase()
        self.assertEqual(order['status'],'pending');self.assertEqual(before,self.balances())
        hot=next(r for r in self.call('dashboard')['stock'] if r['item']=='KG-STOCK-HOT')
        self.assertEqual(hot['on_order'],2)
        self.assertTrue(self.purchase()['already_processed'])
        self.assertEqual(len(self.call('purchases')['orders']),1)
        with self.assertRaisesRegex(ValueError,'different values'):self.purchase(vendor='Someone else')

    def test_partial_receipts_update_cost_pending_and_immutable_history(self):
        self.call('correct_stock',item_code='KG-STOCK-HOT',expected_qty=0,counted_qty=1,valuation_rate=200,comment='Test opening')
        order=self.purchase();first=self.receive(order)
        stock=next(r for r in self.call('dashboard')['stock'] if r['item']=='KG-STOCK-HOT')
        self.assertEqual(stock['balance'],2);self.assertEqual(stock['rate'],300);self.assertEqual(stock['on_order'],1)
        self.assertEqual(self.call('purchases')['orders'][0]['status'],'partial')
        revision=self.store.revision;self.assertTrue(self.receive(order)['already_processed']);self.assertEqual(self.store.revision,revision)
        self.receive(order,request_id='receipt-request-2',bill_ref='TEST-002',items=[dict(item='KG-STOCK-HOT',qty=1,rate=500)])
        saved=self.call('purchases')['orders'][0]
        self.assertEqual(saved['status'],'received');self.assertEqual(saved['receipts'][-1]['details'],first)
        self.assertEqual(self.balances()['KG-STOCK-HOT'],3)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:db.execute("UPDATE purchase_receipts SET details='{}'")

    def test_receipt_rejects_overdelivery_duplicate_reference_and_rolls_back(self):
        order=self.purchase(items=[dict(item='KG-STOCK-HOT',qty=2),dict(item='KG-STOCK-WHITE',qty=1)])
        before=self.balances();revision=self.store.revision
        with self.assertRaisesRegex(ValueError,'exceeds'):
            self.receive(order,items=[dict(item='KG-STOCK-HOT',qty=1,rate=400),dict(item='KG-STOCK-WHITE',qty=2,rate=500)])
        self.assertEqual(before,self.balances());self.assertEqual(self.store.revision,revision)
        self.receive(order)
        with self.assertRaisesRegex(ValueError,'reference'):
            self.receive(order,request_id='different-request')
        with self.assertRaisesRegex(ValueError,'different values'):
            self.receive(order,items=[dict(item='KG-STOCK-HOT',qty=2,rate=400)])

    def test_concurrent_receipt_is_exactly_once_and_cancel_keeps_received_stock(self):
        order=self.purchase()
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(lambda _:self.receive(order),range(2)))
        self.assertEqual(sum(bool(r.get('already_processed')) for r in result),1)
        self.assertEqual(self.balances()['KG-STOCK-HOT'],1)
        self.call('cancel_purchase',purchase=order['name'],comment='Supplier cannot deliver balance')
        self.assertEqual(self.balances()['KG-STOCK-HOT'],1)
        self.assertEqual(next(r for r in self.call('dashboard')['stock'] if r['item']=='KG-STOCK-HOT')['on_order'],0)
        with self.assertRaisesRegex(ValueError,'remaining'):
            self.receive(order,request_id='new-receipt',bill_ref='TEST-002')

    def test_purchase_validates_units_prepared_items_and_prices(self):
        code=self.call('create_ingredient',item_name='Test boxes',uom='nos')['item']
        for rows in [[dict(item=code,qty=1.5)],[dict(item='KG-STOCK-SALAD',qty=1)],[dict(item='KG-STOCK-HOT',qty='NaN')],[dict(item='KG-STOCK-HOT',qty=-1)]]:
            with self.assertRaises(ValueError):self.purchase(items=rows)
        order=self.purchase(items=[dict(item=code,qty=10)])
        for qty,rate in [(1.5,20),(1,''),(1,'Infinity'),(1,-1),(0,20)]:
            with self.assertRaises(ValueError):self.receive(order,items=[dict(item=code,qty=qty,rate=rate)])
        self.assertEqual(self.balances()[code],0)

    def empty_hot_sauce(self):
        self.activate()
        self.call('correct_stock',item_code='KG-STOCK-HOT',expected_qty=10,counted_qty=0,comment='Test sauce ran out')

    def test_without_unavailable_sauce_keeps_main_recipe_and_deducts_actual_items(self):
        self.empty_hot_sauce();original=self.recipe();before=self.balances()
        order=self.call('create_order',qty=2,customizations=[dict(item='KG-STOCK-HOT',action='omit')],kitchen_note='Customer requested no hot sauce')
        movement=self.call('fire_order',order=order['name'])
        self.assertEqual(self.balances()['KG-STOCK-HOT'],0)
        self.assertAlmostEqual(self.balances()['KG-STOCK-WHITE'],before['KG-STOCK-WHITE']-.06)
        self.assertNotIn('KG-STOCK-HOT',[i['item'] for i in movement['ingredients']])
        self.assertEqual(movement['customizations'][0]['name'],'Hot sauce')
        self.assertEqual(movement['kitchen_note'],'Customer requested no hot sauce')
        self.assertEqual(self.recipe()['ingredients'],original['ingredients'])
        before=self.balances();self.call('fire_order',order=order['name']);self.assertEqual(before,self.balances())

    def test_replacement_merges_existing_sauce_and_snapshots_survive_recipe_edits(self):
        self.empty_hot_sauce();original=self.recipe()
        change=dict(item='KG-STOCK-HOT',action='substitute',replacement='KG-STOCK-WHITE',qty=30)
        order=self.call('create_order',qty=2,customizations=json.dumps([change]))
        saved=self.call('orders')[0]
        white=next(i for i in saved['ingredients'] if i['item']=='KG-STOCK-WHITE')
        self.assertEqual(white['qty'],60)
        rows=[dict(i) for i in original['ingredients']];rows[3]['qty']=100
        self.call('save_recipe',ingredients=rows,expected_version=original['latest'])
        self.call('fire_order',order=order['name']);self.assertAlmostEqual(self.balances()['KG-STOCK-WHITE'],9.88)
        self.assertEqual(self.call('orders')[0]['customizations'],saved['customizations'])
        self.assertEqual(self.call('orders')[0]['ingredients'],saved['ingredients'])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:db.execute("UPDATE orders SET customizations='[]'")

    def test_missing_or_insufficient_replacement_does_not_bypass_stock(self):
        self.empty_hot_sauce();before=self.balances()
        plain=self.call('create_order',qty=1)
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.call('fire_order',order=plain['name'])
        order=self.call('create_order',qty=1,customizations=[dict(item='KG-STOCK-HOT',action='substitute',replacement='KG-STOCK-WHITE',qty=20000)])
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.call('fire_order',order=order['name'])
        self.assertEqual(before,self.balances())

    def test_order_customization_validation_and_stale_recipe(self):
        self.activate();r=self.recipe()
        invalid=[dict(item='missing',action='omit'),dict(item='KG-STOCK-HOT',action='substitute',replacement='KG-STOCK-HOT',qty=30),dict(item='KG-STOCK-HOT',action='substitute',replacement='KG-STOCK-WHITE',qty=0),dict(item='KG-STOCK-HOT',action='other')]
        for change in invalid:
            with self.assertRaises(ValueError):self.call('create_order',qty=1,customizations=[change])
        with self.assertRaises(ValueError):self.call('create_order',qty=1,customizations=[dict(item=i['item'],action='omit') for i in r['ingredients']])
        with self.assertRaisesRegex(ValueError,'changed'):self.call('create_order',qty=1,expected_version='old')
        self.assertEqual(self.call('orders'),[])

    def test_waiting_order_revision_preserves_original_and_blocks_old_send(self):
        self.empty_hot_sauce()
        original=self.call('create_order',qty=2)
        saved=self.call('orders')[0]
        current=self.recipe();rows=[dict(r) for r in current['ingredients']];rows=[r for r in rows if r['item']!='KG-STOCK-HOT'];rows[0]['qty']=500
        draft=self.call('save_recipe',ingredients=rows,expected_version=current['latest']);self.call('activate_recipe',version=draft['version'])
        options=self.call('order_recipe',order=original['name'])
        self.assertEqual(options['version'],original['version'])
        revised=self.call('create_order',qty=2,replaces_order=original['name'],expected_version=original['version'],customizations=[dict(item='KG-STOCK-HOT',action='omit')])
        old=next(r for r in self.call('orders') if r['name']==original['name'])
        for field in ('ingredients','version','qty','creation','fired','customizations'):self.assertEqual(old[field],saved[field])
        self.assertEqual(old['superseded_by'],revised['name'])
        with self.assertRaisesRegex(ValueError,'replaced'):self.call('fire_order',order=original['name'])
        self.call('fire_order',order=revised['name'])
        self.assertAlmostEqual(self.balances()['KG-STOCK-POTATO'],9.7)
        with self.assertRaisesRegex(ValueError,'deducted'):self.call('order_recipe',order=revised['name'])
        with self.assertRaisesRegex(ValueError,'replacement'):self.call('create_order',qty=1,replaces_order=original['name'])

    def test_concurrent_revision_only_creates_one_replacement(self):
        self.activate();original=self.call('create_order',qty=1)
        def revise(_):
            try:return self.call('create_order',qty=1,replaces_order=original['name'],customizations=[dict(item='KG-STOCK-HOT',action='omit')])
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(revise,range(2)))
        self.assertEqual(sum(r is not None for r in result),1)
        self.assertEqual(len(self.call('orders')),2)
        self.assertEqual(self.balances()['KG-STOCK-HOT'],10)
