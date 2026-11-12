"""Reusing a stock item must preserve its identity and recorded quantities."""
import json,tempfile,unittest
from pathlib import Path
from server import Store

class ExistingIngredientsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'reuse.sqlite3')
        self.store.dispatch('demo_stock',{})

    def snapshot(self):
        with self.store.connect() as db:
            return {table:[tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ('stock','recipes','movements','purchase_bills')}

    def test_name_code_and_retry_preserve_history_units_and_minimum(self):
        before=self.snapshot()
        for name in (' Hot SAUCE ','KG-STOCK-HOT'):
            result=self.store.dispatch('create_ingredient',dict(item_name=name,uom='Kg',minimum_qty=500))
            self.assertEqual(result['item'],'KG-STOCK-HOT')
            self.assertTrue(result['existed']);self.assertEqual(result['stock_uom'],'L')
        self.assertEqual(before,self.snapshot())

    def test_new_item_and_ambiguous_name_explicit_selection(self):
        first=self.store.dispatch('create_ingredient',dict(item_name='Sample mayo',uom='Kg',minimum_qty=2))
        self.assertFalse(first['existed'])
        with self.store.connect() as db:
            db.execute("INSERT INTO stock VALUES('SECOND-MAYO','Sample mayo','L','ml',3,12,1)")
        with self.assertRaisesRegex(ValueError,'item code'):
            self.store.dispatch('create_ingredient',dict(item_name='Sample mayo',uom='Nos'))
        result=self.store.dispatch('create_ingredient',dict(item_code='SECOND-MAYO',item_name='Sample mayo',uom='Nos'))
        self.assertEqual(result['item'],'SECOND-MAYO');self.assertEqual(result['stock_uom'],'L')

    def test_equipment_missing_item_and_access(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO inventory_sections VALUES('Kurunegala',?)",(json.dumps(dict(revision=1,items={'KG-STOCK-OIL':dict(category='Other Equipment',note='Test')})),))
        options=self.store.dispatch('ingredient_options',{})['items']
        self.assertNotIn('KG-STOCK-OIL',[r['item'] for r in options])
        with self.assertRaisesRegex(ValueError,'Main inventory'):
            self.store.dispatch('create_ingredient',dict(item_name='Cooking oil',uom='L'))
        with self.assertRaisesRegex(ValueError,'no longer'):
            self.store.dispatch('create_ingredient',dict(item_name='Missing',item_code='missing',uom='L'))
        for method in ('create_ingredient','ingredient_options'):
            with self.assertRaisesRegex(ValueError,'manager'):
                self.store.dispatch(method,dict(_actor='Local purchaser'))
            with self.assertRaisesRegex(ValueError,'branch'):
                self.store.dispatch(method,dict(restaurant='Other'))

if __name__=='__main__':unittest.main()
