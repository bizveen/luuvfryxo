import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from server import Store


class ItemNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'names.sqlite3');self.store.dispatch('demo_stock',{})
        self.item='KG-STOCK-HOT'
        self.request=dict(item_code=self.item,item_name='Hot chilli sauce',expected_name='Hot sauce')
    def test_name_only_and_history_unchanged(self):
        bill=self.store.dispatch('native:create_purchase_bill',dict(vendor='Name test',lines=json.dumps([
            dict(item=self.item,qty=2,price=100,unit='L',category='Food / Ingredients')]),_actor='Local purchaser'))['name']
        with self.store.connect() as db:
            before=dict(db.execute('SELECT * FROM stock WHERE item=?',(self.item,)).fetchone())
            recipe=db.execute('SELECT ingredients FROM recipes').fetchone()[0]
            receipt=db.execute('SELECT payload FROM purchase_bills WHERE name=?',(bill,)).fetchone()[0]
        result=self.store.dispatch('rename_inventory_item',self.request)
        self.assertTrue(result['changed'])
        with self.store.connect() as db:
            after=dict(db.execute('SELECT * FROM stock WHERE item=?',(self.item,)).fetchone())
            self.assertEqual(after,dict(before,name='Hot chilli sauce'))
            self.assertEqual(db.execute('SELECT ingredients FROM recipes').fetchone()[0],recipe)
            self.assertEqual(db.execute('SELECT payload FROM purchase_bills WHERE name=?',(bill,)).fetchone()[0],receipt)
        self.assertFalse(self.store.dispatch('rename_inventory_item',self.request)['changed'])
        with self.assertRaisesRegex(ValueError,'another screen'):
            self.store.dispatch('rename_inventory_item',dict(self.request,item_name='Stale edit'))
    def test_validation_and_manager_access(self):
        for name in ('','x','<b>bad</b>','line\nbreak','x'*141):
            with self.assertRaisesRegex(ValueError,'plain-text'):
                self.store.dispatch('rename_inventory_item',dict(self.request,item_name=name))
        with self.assertRaisesRegex(ValueError,'already uses'):
            self.store.dispatch('rename_inventory_item',dict(self.request,item_name='Flour'))
        with self.assertRaisesRegex(ValueError,'manager'):
            self.store.dispatch('rename_inventory_item',dict(self.request,_actor='Local purchaser'))
        with self.assertRaisesRegex(ValueError,'branch'):
            self.store.dispatch('rename_inventory_item',dict(self.request,item_code='unrelated'))


if __name__=='__main__':unittest.main()
