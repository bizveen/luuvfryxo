import json
from pathlib import Path
import tempfile
import unittest

from server import Store, ITEM


class InventoryFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.sqlite3')

    def call(self,method,**params):return self.store.dispatch(method,params)
    def recipe(self):return self.call('recipe',item_code=ITEM)
    def balances(self):return {r['item']:r['balance'] for r in self.call('dashboard')['stock']}
    def activate(self):
        self.call('demo_stock')
        r=self.recipe()
        self.call('activate_recipe',item_code=ITEM,version=r['version'])

    def test_initial_recipe_stays_draft_without_invented_stock(self):
        self.assertEqual(self.recipe()['status'],'Draft')
        self.assertEqual(len(self.recipe()['ingredients']),9)
        self.assertTrue(all(v==0 for v in self.balances().values()))
        with self.assertRaisesRegex(ValueError,'opening stock'):
            self.call('activate_recipe',version=self.recipe()['version'])

    def test_portion_units_and_duplicate_send(self):
        self.activate()
        order=self.call('create_order',qty=2)
        self.call('fire_order',order=order['name'])
        balances=self.balances()
        for row in self.recipe()['ingredients']:
            self.assertAlmostEqual(balances[row['item']],10-row['qty']*2/1000)
        self.assertTrue(self.call('fire_order',order=order['name'])['already_processed'])
        self.assertEqual(self.balances(),balances)

    def test_old_orders_and_old_recipe_unchanged_after_recipe_edit(self):
        self.activate()
        old=self.recipe()
        before=self.call('create_order',qty=1)
        old_rows=json.loads(json.dumps(old['ingredients']))
        rows=json.loads(json.dumps(old_rows));rows[0]['qty']=200
        new=self.call('save_recipe',item_code=ITEM,ingredients=rows,expected_version=old['latest'])
        self.call('activate_recipe',version=new['version'])
        after=self.call('create_order',qty=1)
        self.assertNotEqual(before['version'],after['version'])
        self.call('fire_order',order=before['name'])
        self.assertAlmostEqual(self.balances()['KG-STOCK-POTATO'],9.85)
        self.call('fire_order',order=after['name'])
        self.assertAlmostEqual(self.balances()['KG-STOCK-POTATO'],9.65)
        historical=self.call('recipe',version=old['version'])
        self.assertEqual(historical['ingredients'],old_rows)
        self.assertEqual(self.call('orders')[-1]['ingredients'],old_rows)

    def test_audit_comments_stale_quantity_and_persistence(self):
        params=dict(item_code='KG-STOCK-CHICKEN',counted_qty=5,expected_qty=0,valuation_rate=1500,comment='Opening stock physically counted')
        self.call('correct_stock',**params)
        with self.assertRaisesRegex(ValueError,'Stock changed'):self.call('correct_stock',**params)
        with self.assertRaisesRegex(ValueError,'reason'):self.call('correct_stock',**dict(params,expected_qty=5,counted_qty=4,comment=''))
        audit=self.call('dashboard')['audits'][0]
        self.assertEqual(audit['details']['difference'],5)
        self.assertEqual(audit['details']['comment'],params['comment'])
        reopened=Store(self.store.path)
        self.assertEqual(reopened.dispatch('dashboard',{})['audits'][0],audit)

    def test_stock_shortage_rolls_back_every_ingredient(self):
        self.activate()
        order=self.call('create_order',qty=100)
        before=self.balances()
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.call('fire_order',order=order['name'])
        self.assertEqual(before,self.balances())
        self.assertEqual(self.call('orders')[0]['fired'],0)

    def test_stale_recipe_and_bad_quantities_rejected(self):
        old=self.recipe()
        self.call('save_recipe',ingredients=old['ingredients'],expected_version=old['latest'])
        with self.assertRaisesRegex(ValueError,'another screen'):
            self.call('save_recipe',ingredients=old['ingredients'],expected_version=old['latest'])
        latest=self.recipe()
        for value in ('NaN','Infinity',-1,0):
            rows=[dict(item='KG-STOCK-POTATO',qty=value)]
            with self.assertRaises(ValueError):self.call('save_recipe',ingredients=rows,expected_version=latest['latest'])

    def test_new_ingredient_units_minimum_and_recipe_deduction(self):
        created=self.call('create_ingredient',item_name='Test boxes',uom='nos',minimum_qty=5)
        code=created['item']
        with self.assertRaisesRegex(ValueError,'whole number'):
            self.call('set_minimum',item_code=code,minimum_qty=1.5)
        with self.assertRaisesRegex(ValueError,'whole number'):
            self.call('correct_stock',item_code=code,counted_qty=2.5,expected_qty=0,valuation_rate=10,comment='Test opening count')
        self.call('correct_stock',item_code=code,counted_qty=20,expected_qty=0,valuation_rate=10,comment='Test opening count')
        old=self.recipe()
        saved=self.call('save_recipe',ingredients=[dict(item=code,qty=1)],expected_version=old['latest'])
        self.call('activate_recipe',version=saved['version'])
        order=self.call('create_order',qty=3)
        self.call('fire_order',order=order['name'])
        self.assertEqual(self.balances()[code],17)
        row=next(r for r in self.call('dashboard')['stock'] if r['item']==code)
        self.assertEqual(row['stock_uom'],'nos')
        self.assertEqual(row['minimum_qty'],5)
        reused=self.call('create_ingredient',item_name='test boxes',uom='nos',minimum_qty=5)
        self.assertEqual(reused['item'],code)
        self.assertTrue(reused['existed'])
        self.assertEqual(self.balances()[code],17)

    def test_live_notifications_follow_commits_not_failed_or_duplicate_sends(self):
        initial=self.store.revision
        with self.assertRaises(ValueError):
            self.call('correct_stock',item_code='missing',counted_qty=1,expected_qty=0,comment='Test count')
        self.assertEqual(self.store.revision,initial)
        self.activate()
        order=self.call('create_order',qty=1)
        before=self.store.revision
        self.call('fire_order',order=order['name'])
        self.assertEqual(self.store.revision,before+1)
        self.assertAlmostEqual(self.balances()['KG-STOCK-POTATO'],9.85)
        self.call('fire_order',order=order['name'])
        self.assertEqual(self.store.revision,before+1)


if __name__=='__main__':unittest.main()
