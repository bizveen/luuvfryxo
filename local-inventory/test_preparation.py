import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor

import test_inventory
from server import Store, ITEM


class PreparationFlowTest(unittest.TestCase):
    setUp=test_inventory.InventoryFlowTest.setUp
    call=test_inventory.InventoryFlowTest.call
    recipe=test_inventory.InventoryFlowTest.recipe
    balances=test_inventory.InventoryFlowTest.balances
    activate=test_inventory.InventoryFlowTest.activate
    def setup_salad(self):
        codes=[]
        for name,quantity in [('Cabbage',650),('Carrot',250),('Onion',100)]:
            code=self.call('create_ingredient',item_name=name,uom='Kg')['item']
            self.call('correct_stock',item_code=code,counted_qty=5,expected_qty=0,valuation_rate=200,comment='Sample vegetables for testing')
            codes.append(dict(item=code,qty=quantity))
        recipe=self.call('save_recipe',item_code='KG-STOCK-SALAD',ingredients=codes,output_qty=1000,recipe_note='Sample recipe only',expected_version='')
        self.call('activate_recipe',item_code=recipe['item'],version=recipe['version'])
        return recipe,codes

    def batch(self,recipe,**kwargs):
        p=dict(item_code=recipe['item'],version=recipe['version'],request_id='test-request-0001',planned_qty=1000,actual_qty=1000)
        p.update(kwargs)
        return self.call('complete_batch',**p)

    def test_salad_setup_does_not_invent_recipe_or_stock(self):
        r=self.call('recipe',item_code='KG-STOCK-SALAD')
        self.assertEqual(r['ingredients'],[])
        self.assertEqual(r['version'],'')
        self.assertEqual(r['output_uom'],'g')
        self.assertEqual(r['balance'],0)
        self.assertEqual(self.recipe()['ingredients'][2]['qty'],50)

    def test_batch_then_sale_consumes_prepared_stock_once(self):
        self.activate()
        recipe,raw=self.setup_salad()
        before=self.balances()
        receipt=self.batch(recipe,planned_qty=2000,actual_qty=1900,comment='Sample trimming yield')
        after=self.balances()
        self.assertEqual(after['KG-STOCK-SALAD'],before['KG-STOCK-SALAD']+1.9)
        for row in raw:self.assertAlmostEqual(after[row['item']],before[row['item']]-row['qty']*2/1000)
        self.assertEqual(receipt['cost'],400)
        order=self.call('create_order',qty=2)
        self.assertNotEqual(order['version'],recipe['version'])
        self.call('fire_order',order=order['name'])
        self.assertAlmostEqual(self.balances()['KG-STOCK-SALAD'],after['KG-STOCK-SALAD']-.1)
        for row in raw:self.assertEqual(self.balances()[row['item']],after[row['item']])
        self.call('fire_order',order=order['name'])
        self.assertAlmostEqual(self.balances()['KG-STOCK-SALAD'],after['KG-STOCK-SALAD']-.1)

    def test_batch_retry_concurrency_and_payload_mismatch(self):
        recipe,raw=self.setup_salad();revision=self.store.revision
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.batch(recipe),range(2)))
        self.assertEqual(sum(bool(r.get('already_processed')) for r in results),1)
        self.assertEqual(self.store.revision,revision+1)
        self.assertEqual(self.balances()['KG-STOCK-SALAD'],1)
        with self.assertRaisesRegex(ValueError,'different values'):self.batch(recipe,planned_qty=2000,actual_qty=2000)

    def test_failed_batches_are_atomic_and_do_not_publish(self):
        recipe,raw=self.setup_salad();before=self.balances();revision=self.store.revision
        for params in [dict(planned_qty=100000,actual_qty=100000),dict(actual_qty=900),dict(actual_qty=0),dict(planned_qty='NaN'),dict(version='bad-version'),dict(actual_qty='Infinity')]:
            with self.assertRaises(ValueError):self.batch(recipe,**params)
            self.assertEqual(self.balances(),before)
            self.assertEqual(self.store.revision,revision)
        self.assertEqual(self.call('recipe',item_code=recipe['item'])['batches'],[])

    def test_recipe_and_batch_history_survives_new_versions_and_restart(self):
        recipe,raw=self.setup_salad();receipt=self.batch(recipe)
        old=self.call('recipe',item_code=recipe['item']);old_batch=old['batches'][0]
        changed=self.call('save_recipe',item_code=recipe['item'],ingredients=[dict(item=raw[0]['item'],qty=800)],output_qty=700,recipe_note='Different recipe',expected_version=recipe['version'])
        self.call('activate_recipe',item_code=recipe['item'],version=changed['version'])
        with self.assertRaisesRegex(ValueError,'changed'):self.batch(recipe,request_id='another-request')
        self.assertTrue(self.batch(recipe)['already_processed'])
        historical=self.call('recipe',item_code=recipe['item'],version=recipe['version'])
        self.assertEqual(historical['ingredients'],recipe['ingredients'])
        self.assertEqual(historical['output_qty'],1000)
        self.assertEqual(historical['recipe_note'],'Sample recipe only')
        self.assertEqual(historical['batches'][0],old_batch)
        reopened=Store(self.store.path)
        self.assertEqual(reopened.dispatch('recipe',dict(item_code=recipe['item']))['batches'][0],old_batch)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:db.execute('UPDATE batches SET details=?',(json.dumps({}),))
        with self.assertRaises(ValueError):self.call('recipe',item_code=ITEM,version=recipe['version'])

    def test_prepared_components_do_not_expand_into_raw_ingredients(self):
        salad,raw=self.setup_salad();self.batch(salad)
        code=self.call('create_ingredient',item_name='Salad cups',uom='nos')['item']
        self.call('create_prepared_recipe',item_code=code)
        r=self.call('save_recipe',item_code=code,expected_version='',ingredients=[dict(item=salad['item'],qty=100)],output_qty=1)
        self.call('activate_recipe',item_code=code,version=r['version'])
        before=self.balances()
        self.batch(r,request_id='cups-request',planned_qty=3,actual_qty=3)
        self.assertAlmostEqual(self.balances()[salad['item']],.7)
        self.assertEqual(self.balances()[code],3)
        for row in raw:self.assertEqual(self.balances()[row['item']],before[row['item']])
        with self.assertRaisesRegex(ValueError,'whole number'):self.batch(r,request_id='cups-fraction',planned_qty=1.5,actual_qty=1.5)
        with self.assertRaisesRegex(ValueError,'itself'):
            self.call('save_recipe',item_code=salad['item'],expected_version=salad['version'],ingredients=[dict(item=code,qty=1)],output_qty=100)
        with self.assertRaisesRegex(ValueError,'itself'):
            self.call('save_recipe',item_code=salad['item'],expected_version=salad['version'],ingredients=[dict(item=salad['item'],qty=100)],output_qty=100)

    def test_migration_keeps_previous_orders_and_counts(self):
        path=self.store.path.parent/'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE stock(item TEXT PRIMARY KEY,name TEXT,stock_uom TEXT,uom TEXT,balance REAL NOT NULL,rate REAL NOT NULL);
                CREATE TABLE recipes(name TEXT PRIMARY KEY,creation TEXT NOT NULL,docstatus INTEGER NOT NULL,owner TEXT,ingredients TEXT NOT NULL,effective_from TEXT);
                CREATE TABLE orders(name TEXT PRIMARY KEY,creation TEXT,qty INTEGER NOT NULL,version TEXT NOT NULL,ingredients TEXT NOT NULL,fired INTEGER NOT NULL DEFAULT 0);
                INSERT INTO stock VALUES('KG-STOCK-SALAD','Prepared salad','Kg','g',0.9,200);
                INSERT INTO recipes VALUES('LOCAL-RECIPE-0001','2026-01-01',1,'Manager','[]','2026-01-01');
                INSERT INTO orders VALUES('OLD-ORDER','2026-01-02',1,'LOCAL-RECIPE-0001','[]',1);
            ''')
        db.close()
        migrated=Store(path)
        self.assertEqual(migrated.dispatch('dashboard',{})['stock'][0]['balance'],.9)
        self.assertEqual(migrated.dispatch('orders',{})[0]['name'],'OLD-ORDER')
        self.assertEqual(migrated.dispatch('recipe',{})['version'],'LOCAL-RECIPE-0001')
        self.assertEqual(migrated.dispatch('recipe',dict(item_code='KG-STOCK-SALAD'))['ingredients'],[])
