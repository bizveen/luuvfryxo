"""Counted packaging must preserve ingredient quantities and previous drafts."""
import sys,unittest
from copy import deepcopy
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'deploy')]
from confirm_loaded_fries_packaging import ITEMS,PACKAGING,corrected,draft_note
from luuvcrm.recipe_sheets import references,initial,changed,view


class LoadedFriesPackagingTest(unittest.TestCase):
    def test_five_loaded_fries_keep_food_and_previous_versions(self):
        self.assertEqual(len(ITEMS),5)
        for item in ITEMS:
            ref=references('Kurunegala')[item];history=[initial(ref)];before=deepcopy(history)
            rows=corrected(history[-1]['ingredients'])
            self.assertEqual(rows[:-4],before[0]['ingredients'])
            self.assertEqual(rows[-4:],[dict(name=n,qty=q,unit='Nos') for n,q,a in PACKAGING])
            revision=changed(history,rows,draft_note(history[-1]['note']),0,'Test manager','test')
            self.assertEqual(history,before)
            saved=history+[revision]
            self.assertIsNone(changed(saved,corrected(rows),draft_note(revision['note']),revision['revision'],'Test manager','test'))
            self.assertIn('Packaging quantities are specified in Nos; link each component to its stock item.',view(ref,saved)['pending_notes'])
            self.assertEqual(ref['packaging']['qty'],1)
            self.assertEqual(ref['packaging']['unit'],'set')

    def test_existing_aliases_updated_without_double_packaging(self):
        rows=[dict(name='French fries',qty=200,unit='g'),dict(name='Loaded fries boxes',qty=3,unit='Nos'),
            dict(name='Spoons',qty=1,unit='Nos'),dict(name='Forks',qty=1,unit='Nos'),dict(name='Serviette paper',qty=1,unit='Nos')]
        result=corrected(rows)
        self.assertEqual(len(result),5)
        self.assertEqual([r['qty'] for r in result],[200,1,2,2,2])
        self.assertEqual(corrected(result),result)
        with self.assertRaises(ValueError):corrected(rows+[dict(name='Wooden fork',qty=2,unit='Nos')])
        with self.assertRaises(ValueError):corrected([dict(name='Packaging',qty=1,unit='set')])
