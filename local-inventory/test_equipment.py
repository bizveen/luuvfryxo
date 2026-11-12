import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from server import Store


class EquipmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'equipment.sqlite3')
        self.item=self.store.dispatch('create_ingredient',dict(item_name='Sample Water Jug',uom='nos',minimum_qty=5))['item']
        self.store.dispatch('correct_stock',dict(item_code=self.item,expected_qty=0,counted_qty=2,valuation_rate=530,comment='Sample equipment count'))
    def test_move_preserves_stock_and_excludes_reorder_then_restores(self):
        before=self.store.dispatch('dashboard',{})
        move=dict(item_code=self.item,section='main',category='Kitchen Equipment',expected_revision=0,comment='Reusable equipment')
        self.store.dispatch('move_inventory_item',move)
        after=self.store.dispatch('dashboard',{})
        self.assertNotIn(self.item,[r['item'] for r in after['stock']])
        self.assertEqual(after['main_inventory'][0]['balance'],2)
        self.assertNotIn(self.item,[r['item'] for r in self.store.dispatch('restock',{})['items']])
        self.assertEqual(before['audits'],after['audits'])
        with self.assertRaisesRegex(ValueError,'sections changed'):self.store.dispatch('move_inventory_item',move)
        self.store.dispatch('move_inventory_item',dict(move,section='running',expected_revision=1))
        self.assertEqual(next(r for r in self.store.dispatch('dashboard',{})['stock'] if r['item']==self.item)['balance'],2)
    def test_menu_recipe_and_purchaser_cannot_move(self):
        move=dict(item_code='KG-STOCK-HOT',section='main',category='Kitchen Equipment',expected_revision=0,comment='Test validation')
        with self.assertRaisesRegex(ValueError,'recipe'):self.store.dispatch('move_inventory_item',move)
        with self.assertRaisesRegex(ValueError,'manager'):self.store.dispatch('move_inventory_item',dict(move,item_code=self.item,_actor='Local purchaser'))
        self.assertEqual(self.store.dispatch('dashboard',{})['section_revision'],0)


if __name__=='__main__':unittest.main()
