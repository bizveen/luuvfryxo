"""Three fruit recipes share a base while old drafts and stock stay unchanged."""
import sys,tempfile,unittest
from copy import deepcopy
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'deploy')]
from server import Store
from luuvcrm.recipe_sheets import references
from configure_lemonade_recipes import updated_rows,updated_note


class LemonadeRecipeTest(unittest.TestCase):
    def test_shared_base_and_history_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'preview.sqlite3';store=Store(path)
            stock=store.dispatch('dashboard',{})['stock']
            refs={code:ref for code,ref in references('Kurunegala').items() if ref.get('beverage')}
            fruits={'KG-LEMONADE-STRAWBERRY':('Strawberry pulp',60,'ml'),
                    'KG-LEMONADE-PASSION':('Passion pulp',60,'ml'),
                    'KG-LEMONADE-BLUEBERRY':('Blueberry',20,'g')}
            self.assertEqual(set(refs),set(fruits))
            shared=[('Ice',50,'g'),('Sprite',280,'ml'),('Lime',20,'g'),('Clear cups',1,'Nos'),
                    ('Juice lid',1,'Nos'),('Straw',1,'Nos'),('Juice bag',1,'Nos')]
            for code,ref in refs.items():
                before=deepcopy(store.dispatch('recipe',dict(item_code=code)))
                current=before['sheet_draft']['current']
                rows=updated_rows(current['ingredients'],ref);note=updated_note(current['note'],ref)
                self.assertEqual([(r['name'],r['qty'],r['unit']) for r in rows],[fruits[code]]+shared)
                args=dict(item_code=code,ingredients=rows,note=note,expected_revision=current['revision'])
                saved=store.dispatch('save_sheet_draft',args)
                self.assertEqual(saved['history'][:-1],before['sheet_draft']['history'])
                self.assertEqual(saved['current']['revision'],1)
                self.assertNotIn('still to be supplied',saved['current']['note'])
                self.assertTrue(any('Confirm Sprite quantity' in n for n in saved['pending_notes']))
                if code.endswith('BLUEBERRY'):self.assertTrue(any('Confirm Blueberry quantity' in n for n in saved['pending_notes']))
                self.assertFalse(any('sauce' in n.lower() or 'salad' in n.lower() for n in saved['pending_notes']))
                args['expected_revision']=1
                self.assertEqual(store.dispatch('save_sheet_draft',args)['history'],saved['history'])
                reloaded=Store(path).dispatch('recipe',dict(item_code=code))
                self.assertEqual(reloaded['sheet_draft']['history'],saved['history'])
                self.assertEqual(reloaded['ingredients'],before['ingredients'])
                self.assertEqual(reloaded['version'],before['version'])
            self.assertEqual(store.dispatch('dashboard',{})['stock'],stock)
            lemon=store.dispatch('recipe',dict(item_code='KG-LEMON-MINT'))['sheet_draft']
            self.assertEqual(lemon['current']['revision'],0)
            self.assertEqual(len(lemon['current']['ingredients']),3)

    def test_extra_rows_and_notes_preserved_without_duplicate_components(self):
        ref=references('Kurunegala')['KG-LEMONADE-STRAWBERRY']
        rows=[dict(name='Manager addition',qty=2,unit='g'),dict(name='Clear cups',qty=2,unit='Nos')]
        original=deepcopy(rows);result=updated_rows(rows,ref)
        self.assertEqual(rows,original)
        self.assertEqual(result[-1],rows[0])
        self.assertEqual(updated_rows(result,ref),result)
        note=updated_note('Keep this manager note.',ref)
        self.assertTrue(note.startswith('Keep this manager note.'))
        self.assertEqual(updated_note(note,ref),note)
        with self.assertRaises(ValueError):updated_rows(rows+[dict(name='clear cups',qty=1,unit='Nos')],ref)
        with self.assertRaises(ValueError):updated_rows([dict(name='Packaging',qty=1,unit='set')],ref)
