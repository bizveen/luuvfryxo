"""Supplied recipes are visible, editable and independent of stock history."""
import json,sqlite3,tempfile,unittest
from copy import deepcopy
from pathlib import Path
from server import Store
from luuvcrm.recipe_sheets import references

class RecipeSheetTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'recipes.sqlite3';self.store=Store(self.path)
    def test_all_seven_visible_without_stock_creation(self):
        data=self.store.dispatch('dashboard',{})
        self.assertEqual(len([r for r in data['menu'] if r['has_sheet_draft']]),7)
        self.assertEqual(len(data['stock']),9)
        total=0
        for item in references('Kurunegala'):
            draft=self.store.dispatch('recipe',dict(item_code=item))['sheet_draft']
            total+=len(draft['current']['ingredients'])
            self.assertEqual(draft['current']['revision'],0)
        self.assertEqual(total,49)
        fish=self.store.dispatch('recipe',dict(item_code='KG-CHIPS-FISH'))['sheet_draft']
        self.assertEqual(fish['current']['ingredients'][-1]['unit'],'portion')
        spicy=self.store.dispatch('recipe',dict(item_code='KG-CHIPS-SPICY'))['sheet_draft']
        self.assertEqual(spicy['current']['ingredients'][-1]['qty'],50)
        self.assertTrue(any('split' in n for n in spicy['pending_notes']))
    def test_edit_retry_reopen_and_previous_history(self):
        item='KG-FRIES-CHICKEN';original=self.store.dispatch('recipe',dict(item_code=item))
        with self.store.connect() as db:
            before={t:db.execute('SELECT * FROM '+t).fetchall() for t in ('stock','recipes','orders','movements')}
        rows=deepcopy(original['sheet_draft']['current']['ingredients']);rows[0]['qty']=210
        request=dict(item_code=item,ingredients=rows,note='Measured portion',expected_revision=0)
        result=self.store.dispatch('save_sheet_draft',request)
        self.assertEqual(result['current']['revision'],1)
        self.assertEqual(result['history'][0]['ingredients'][0]['qty'],200)
        self.assertEqual(self.store.dispatch('save_sheet_draft',request),result)
        reopened=Store(self.path).dispatch('recipe',dict(item_code=item))
        self.assertEqual(reopened['sheet_draft']['current']['ingredients'][0]['qty'],210)
        with self.store.connect() as db:
            for t,old in before.items():self.assertEqual(db.execute('SELECT * FROM '+t).fetchall(),old)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                db.execute("UPDATE recipe_sheet_versions SET payload='{}' WHERE item=?",(item,))
        with self.assertRaisesRegex(ValueError,'another screen'):
            self.store.dispatch('save_sheet_draft',dict(request,note='Stale edit'))
    def test_units_quantities_permissions_and_branch(self):
        item='KG-FRIES-CHICKEN';rows=self.store.dispatch('recipe',dict(item_code=item))['sheet_draft']['current']['ingredients']
        for qty,unit in [(float('nan'),'g'),(-1,'g'),(1,'unlisted'),(1.5,'pieces')]:
            changed=deepcopy(rows);changed[0].update(qty=qty,unit=unit)
            with self.assertRaises(ValueError):self.store.dispatch('save_sheet_draft',dict(item_code=item,ingredients=changed,expected_revision=0))
        with self.assertRaisesRegex(ValueError,'manager'):
            self.store.dispatch('save_sheet_draft',dict(_actor='Local purchaser'))
        with self.assertRaisesRegex(ValueError,'branch'):
            self.store.dispatch('save_sheet_draft',dict(restaurant='Other'))

if __name__=='__main__':unittest.main()
