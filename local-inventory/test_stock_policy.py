import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from server import Store
from luuvcrm.stock_policy import normalize, assess


class StockPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.sqlite3');self.call('demo_stock')
        recipe=self.call('recipe');self.call('activate_recipe',version=recipe['version'])
        self.hot='KG-STOCK-HOT';self.chicken='KG-STOCK-CHICKEN'

    def call(self,method,**p):return self.store.dispatch(method,p)
    def stock(self):return {r['item']:r for r in self.call('dashboard')['stock']}
    def count(self,item,quantity):
        return self.call('correct_stock',item_code=item,expected_qty=self.stock()[item]['balance'],counted_qty=quantity,comment='Physical test count')
    def policy(self,items,default=None):
        current=self.call('dashboard')['stock_policy']
        return self.call('save_stock_policy',policy=dict(revision=current['revision'],default=default or dict(mode='block'),items=items))
    def order(self,qty=1,**kw):return self.call('create_order',qty=qty,**kw)['name']
    def fire(self,order):return self.call('fire_order',order=order)

    def test_default_blocks_and_rolls_back_every_item(self):
        self.count(self.hot,0);order=self.order();before=self.stock()
        with self.assertRaisesRegex(ValueError,'Insufficient stock'):self.fire(order)
        self.assertEqual(before,self.stock());self.assertFalse(self.call('orders')[0]['fired'])

    def test_item_percent_inclusive_boundary_and_persistent_deficit(self):
        self.policy({self.chicken:dict(mode='percent',percent=10)})
        self.count(self.chicken,.135)
        result=self.fire(self.order())
        self.assertEqual(self.stock()[self.chicken]['balance'],-.015)
        self.assertEqual(result['warnings'][0]['limit'],.015)
        self.assertEqual(result['warnings'][0]['percent'],10)
        before=self.stock()
        with self.assertRaisesRegex(ValueError,'allowed minus'):self.fire(self.order())
        self.assertEqual(before,self.stock())

    def test_outside_limit_and_other_ingredient_still_block(self):
        self.policy({self.chicken:dict(mode='percent',percent=10)})
        self.count(self.chicken,.134999)
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.fire(self.order())
        self.count(self.chicken,.15);self.count(self.hot,0)
        with self.assertRaisesRegex(ValueError,'Hot sauce'):self.fire(self.order())

    def test_portions_scale_recipe_percentage(self):
        self.policy({self.chicken:dict(mode='percent',percent=10)})
        self.count(self.chicken,1.35)
        result=self.fire(self.order(10))
        self.assertEqual(self.stock()[self.chicken]['balance'],-.15)
        self.assertEqual(result['warnings'][0]['limit'],.15)

    def test_unlimited_negative_and_historical_snapshot(self):
        self.policy({self.hot:dict(mode='negative')});self.count(self.hot,0)
        order=self.order(2);first=self.fire(order)
        self.assertEqual(self.stock()[self.hot]['balance'],-.06)
        self.policy({self.hot:dict(mode='block')})
        before=self.stock();retry=self.fire(order)
        self.assertTrue(retry['already_processed']);self.assertEqual(retry['warnings'],first['warnings'])
        self.assertEqual(retry['policy'],first['policy']);self.assertEqual(before,self.stock())
        self.assertEqual(self.call('dashboard')['shortage_sales'][0]['policy'],first['policy'])

    def test_substitutions_use_actual_replacement_recipe_usage(self):
        white='KG-STOCK-WHITE'
        self.policy({white:dict(mode='percent',percent=10)})
        self.count(white,.054);self.count(self.hot,0)
        order=self.order(customizations=[dict(item=self.hot,action='substitute',replacement=white,qty=30)])
        result=self.fire(order)
        self.assertEqual(self.stock()[white]['balance'],-.006)
        self.assertEqual(result['warnings'][0]['qty'],.06)
        self.assertEqual(self.stock()[self.hot]['balance'],0)

    def test_omitted_item_never_needs_permission(self):
        self.count(self.hot,0)
        result=self.fire(self.order(customizations=[dict(item=self.hot,action='omit')]))
        self.assertEqual(result['warnings'],[]);self.assertEqual(self.stock()[self.hot]['balance'],0)

    def test_receipt_can_partially_clear_negative_and_reach_zero(self):
        self.policy({self.hot:dict(mode='negative')});self.count(self.hot,0);self.fire(self.order(2))
        self.policy({})
        for expected in (-.03,0,.03):
            bill=self.call('native:create_purchase_bill',vendor='Sample supplier',lines=json.dumps([
                dict(item=self.hot,qty=.03,price=1234,unit='L',category='Food / Ingredients')]))
            self.call('native:resolve_approval',name=bill['name'],decision='approved',_actor='Local manager')
            self.assertAlmostEqual(self.stock()[self.hot]['balance'],expected)
            self.assertEqual(self.stock()[self.hot]['rate'],1234)
            self.assertTrue(math.isfinite(self.stock()[self.hot]['rate']))

    def test_audit_recovers_negative_with_reason(self):
        self.policy({self.hot:dict(mode='negative')});self.count(self.hot,0);self.fire(self.order())
        audit=self.count(self.hot,0)
        self.assertEqual(audit['details']['before'],-.03)
        self.assertEqual(audit['details']['difference'],.03)

    def test_concurrent_sales_cannot_both_use_one_tolerance(self):
        self.policy({self.hot:dict(mode='percent',percent=10)});self.count(self.hot,.027)
        orders=[self.order(),self.order()]
        def attempt(name):
            try:return self.fire(name)
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,orders))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.stock()[self.hot]['balance'],-.003)

    def test_branch_isolation_permissions_and_stale_settings(self):
        other=normalize(dict(default=dict(mode='negative')))
        with self.store.connect() as db:db.execute('INSERT INTO branch_stock_policies VALUES(?,?)',('Sample other branch',json.dumps(other)))
        stale=self.call('dashboard')['stock_policy'];self.policy({self.hot:dict(mode='percent',percent=25)})
        with self.assertRaisesRegex(ValueError,'another screen'):self.call('save_stock_policy',policy=stale)
        with self.assertRaisesRegex(ValueError,'Only Manager'):self.call('save_stock_policy',policy=stale,_actor='Local purchaser')
        with self.assertRaisesRegex(ValueError,'Switch to that branch'):self.call('save_stock_policy',restaurant='Sample other branch',policy=other)
        with self.store.connect() as db:self.assertEqual(self.store.stock_policy(db,'Sample other branch'),other)

    def test_negative_without_moq_still_on_reorder_list(self):
        self.policy({self.hot:dict(mode='negative')});self.count(self.hot,0);self.fire(self.order())
        row=next(r for r in self.call('restock')['items'] if r['item']==self.hot)
        self.assertEqual(row['minimum_qty'],0);self.assertEqual(row['suggested_qty'],.03)

    def test_nos_uses_whole_deductions_not_rounded_tolerance(self):
        row=dict(item='BOX',name='Box',before=0,qty=1,uom='nos')
        self.assertTrue(assess(dict(default=dict(mode='percent',percent=10)),[row])['blocked'])
        self.assertTrue(assess(dict(default=dict(mode='percent',percent=100)),[row])['warnings'])

    def test_invalid_percent_and_unknown_items_rejected(self):
        for percent in (-1,101,'NaN','Infinity',''):
            with self.assertRaises(ValueError):self.policy({self.hot:dict(mode='percent',percent=percent)})
        with self.assertRaisesRegex(ValueError,'enabled stock'):self.policy({'MISSING':dict(mode='negative')})


if __name__=='__main__':unittest.main()
