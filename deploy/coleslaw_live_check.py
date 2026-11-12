"""Read back only the coleslaw setup and the seven requested recipe quantities."""
import frappe
from unittest.mock import patch
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch
    from luuvcrm.api import manager_inventory as manager
    with patch.object(branch,'selected',return_value='Kurunegala'):
        data=manager.dashboard()
        assert data['preparation_recipes']
        salad=manager.recipe('KG-STOCK-COLESLAW')
        assert salad['kind']=='prepared' and salad['sheet_draft']['reference']['sample']
        assert salad['version']=='' and salad['balance']==0
        sauces=0
        for item in ('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN','KG-FRIES-BEEF','KG-FRIES-SAUSAGE','KG-CHIPS-FISH','KG-CHIPS-SPICY'):
            draft=manager.recipe(item)['sheet_draft']
            rows=draft['current']['ingredients']
            spices=[r for r in rows if 'seasoning' in r['name'].lower()]
            assert len(spices)==1 and spices[0]['qty']==1 and spices[0]['unit']=='g'
            oil=[r for r in rows if r['name']=='Frying oil']
            assert len(oil)==1 and oil[0]['qty']==50 and oil[0]['unit']=='ml'
            for row in rows:
                if row['name'].lower() in ('hot sauce','bbq sauce','garlic mayo','mustard mayo'):
                    assert row['qty']==30 and row['unit']=='g';sauces+=1
            if item in ('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN'):
                assert draft['preparations']==[dict(item='KG-STOCK-COLESLAW',name='Coleslaw salad',qty=50,unit='g')]
        assert sauces==24
        print('PASS live coleslaw draft, preparation links, seven seasoning allocations, 24 sauce quantities and seven oil estimates; no sample recipe activated or stock invented')
finally:frappe.db.rollback();frappe.destroy()
