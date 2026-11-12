"""Local adapter regression: unit-only edits and prior stock preservation."""
import tempfile,unittest
from pathlib import Path
from server import Store

class ItemUnitsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'units.sqlite3')
    def test_unused_unit_edit_and_retry_preserve_identity(self):
        item=self.store.dispatch('create_ingredient',dict(item_name='Sample flour',uom='Nos'))['item']
        request=dict(item_code=item,item_name='Sample flour',expected_name='Sample flour',unit='Kg',expected_unit='Nos')
        self.assertTrue(self.store.dispatch('rename_inventory_item',request)['changed'])
        self.assertFalse(self.store.dispatch('rename_inventory_item',request)['changed'])
        info=self.store.dispatch('inventory_item_units',dict(item_code=item))
        self.assertEqual(info['unit'],'Kg');self.assertEqual(len(info['history']),1)
        with self.assertRaisesRegex(ValueError,'Reopen'):
            self.store.dispatch('rename_inventory_item',dict(request,unit='g'))
    def test_used_quantity_cannot_be_relabelled_and_name_can_change(self):
        self.store.dispatch('demo_stock',{})
        row=self.store.dispatch('dashboard',{})['stock'][0]
        request=dict(item_code=row['item'],item_name=row['name']+' corrected',expected_name=row['name'],unit='Box',expected_unit=row['stock_uom'])
        with self.assertRaisesRegex(ValueError,'locked'):self.store.dispatch('rename_inventory_item',request)
        fresh=next(r for r in self.store.dispatch('dashboard',{})['stock'] if r['item']==row['item'])
        self.assertEqual(fresh,row)
        self.store.dispatch('rename_inventory_item',dict(request,unit=row['stock_uom']))
        fresh=next(r for r in self.store.dispatch('dashboard',{})['stock'] if r['item']==row['item'])
        self.assertEqual(fresh['balance'],row['balance']);self.assertEqual(fresh['stock_uom'],row['stock_uom'])

if __name__=='__main__':unittest.main()
