"""The seasoning correction changes new drafts only and is safe to retry."""
import sys,unittest
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
from configure_coleslaw_and_seasoning import seasoned,ITEMS,note_with_seasoning
from luuvcrm.recipe_sheets import references


class ColeslawSetupTest(unittest.TestCase):
    def test_one_gram_seasoning_preserves_other_rows_and_source(self):
        for item in ITEMS:
            original=references('Kurunegala')[item]['ingredients']
            snapshot=deepcopy(original)
            rows=seasoned(original)
            self.assertEqual(original,snapshot)
            changed=[r for r in rows if 'seasoning' in r['name'].lower()]
            self.assertEqual(len(changed),1)
            self.assertEqual((changed[0]['qty'],changed[0]['unit']),(1,'g'))
            self.assertEqual([r for r in rows if r not in changed],
                [r for r in original if 'seasoning' not in r['name'].lower()])
            self.assertEqual(seasoned(rows),rows)
        note=note_with_seasoning('Existing note')
        self.assertEqual(note_with_seasoning(note),note)

    def test_ambiguous_multiple_seasonings_need_review(self):
        with self.assertRaises(ValueError):
            seasoned([dict(name='Seasoning',qty=1,unit='g'),dict(name='Spicy seasoning',qty=5,unit='g')])
