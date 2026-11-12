"""Juice packaging is counted, editable and remains an incomplete draft."""
import sys,tempfile,unittest
from copy import deepcopy
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'deploy')]
from server import Store
from luuvcrm.recipe_sheets import references
from confirm_juice_packaging import ALIASES,corrected,draft_note


class JuicePackagingTest(unittest.TestCase):
    def test_juice_drafts_visible_without_inventing_stock_or_full_recipes(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'preview.sqlite3')
            refs={c:r for c,r in references('Kurunegala').items() if r.get('packaging_only')}
            self.assertEqual(set(refs),{'KG-LEMON-MINT','KG-LEMONADE-PASSION','KG-LEMONADE-STRAWBERRY','KG-LEMONADE-BLUEBERRY'})
            for code in refs:
                recipe=store.dispatch('recipe',dict(item_code=code))
                self.assertEqual(recipe['version'],'')
                self.assertEqual(recipe['ingredients'],[])
                sheet=recipe['sheet_draft']
                self.assertEqual(sheet['current']['ingredients'],[dict(name=n,qty=1,unit='Nos') for n in ALIASES])
                self.assertTrue(any('beverage ingredients' in note for note in sheet['pending_notes']))
                self.assertFalse(any('sauce' in note.lower() or 'salad' in note.lower() for note in sheet['pending_notes']))
                before=deepcopy(sheet['history']);rows=sheet['current']['ingredients']+[dict(name='Test beverage ingredient',qty=50,unit='ml')]
                saved=store.dispatch('save_sheet_draft',dict(item_code=code,ingredients=rows,note='Measured test draft',expected_revision=0))
                self.assertEqual(saved['history'][:-1],before)
                self.assertEqual(store.dispatch('recipe',dict(item_code=code))['ingredients'],[])
            self.assertEqual(len(store.dispatch('dashboard',{})['stock']),10)
            self.assertNotIn('KG-ICED-COFFEE',refs)

    def test_existing_beverage_rows_and_retry_preserved(self):
        source=[dict(name='Lemon juice',qty=40,unit='ml'),dict(name='Cup',qty=2,unit='Nos')]
        before=deepcopy(source);updated=corrected(source)
        self.assertEqual(source,before)
        self.assertEqual(updated[0],source[0])
        self.assertEqual(updated[1:],[dict(name=n,qty=1,unit='Nos') for n in ALIASES])
        self.assertEqual(corrected(updated),updated)
        self.assertEqual(draft_note(draft_note('Keep this note')),draft_note('Keep this note'))
        with self.assertRaises(ValueError):corrected(source+[dict(name='Clear cups',qty=1,unit='Nos')])
