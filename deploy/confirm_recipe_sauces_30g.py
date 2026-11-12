"""Append the owner's 30 g sauce correction; retain all existing draft history."""
from copy import deepcopy

COMBINED='pending'  # pending, each_30, or total_30; needs the owner's answer.
SAUCES={'hot sauce','bbq sauce','garlic mayo','mustard mayo'}
ITEMS=('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN','KG-FRIES-BEEF',
       'KG-FRIES-SAUSAGE','KG-CHIPS-FISH','KG-CHIPS-SPICY')
NOTE='Owner confirmed individually listed sauces at 30 g each on 2026-11-12.'


def corrected(item,ingredients,combined=COMBINED):
    rows=deepcopy(ingredients)
    if item=='KG-CHIPS-SPICY':
        if combined=='pending':return rows
        indexes=[i for i,r in enumerate(rows) if r['name'].lower()=='bbq sauce & hot sauce']
        if not indexes:
            if combined=='each_30' and {r['name'].lower() for r in rows if r['name'].lower() in SAUCES}=={'bbq sauce','hot sauce'}:
                for row in rows:
                    if row['name'].lower() in SAUCES:row.update(qty=30,unit='g')
                return rows
            raise ValueError('Recheck the combined sauce row before updating Spicy Chips.')
        assert len(indexes)==1
        index=indexes[0]
        if combined=='total_30':rows[index].update(qty=30,unit='g')
        elif combined=='each_30':rows[index:index+1]=[dict(name='BBQ sauce',qty=30,unit='g'),dict(name='Hot sauce',qty=30,unit='g')]
        else:raise ValueError('Unknown combined-sauce decision')
        return rows
    assert item in ITEMS
    matches=[r for r in rows if r['name'].lower() in SAUCES]
    expected=2 if item=='KG-CHIPS-FISH' else 4
    assert len(matches)==expected,'Recipe sauce names changed; review '+item
    for row in matches:row.update(qty=30,unit='g')
    return rows


def run_native():
    import frappe
    from unittest.mock import patch
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:
        from luuvcrm import branch
        from luuvcrm.api import manager_inventory as manager
        from luuvcrm.api.recipe_sheets import stored
        with patch.object(branch,'selected',return_value='Kurunegala'):
            warehouse=branch.warehouse('Kurunegala');before=deepcopy(stored(warehouse,lock=True));changed=[]
            for item in ITEMS:
                history=before[item];latest=history[-1]
                rows=corrected(item,latest['ingredients'])
                if rows==latest['ingredients']:continue
                note=latest.get('note','')
                addition=NOTE if item!='KG-CHIPS-SPICY' else ('Owner confirmed BBQ sauce and hot sauce at 30 g each.' if COMBINED=='each_30' else 'Owner confirmed the combined BBQ/hot sauce quantity at 30 g total.')
                if addition not in note and len(note)+len(addition)+1<=1000:note=(note+'\n'+addition).strip()
                saved=manager.save_sheet_draft(item,rows,latest['revision'],note,restaurant='Kurunegala')
                assert saved['history'][:-1]==history
                assert saved['current']['ingredients']==rows
                changed.append(dict(item=item,revision=saved['current']['revision']))
            after=stored(warehouse)
            for item in before:
                if item not in {r['item'] for r in changed}:assert after[item]==before[item]
            frappe.db.commit()
            # Reload through the same endpoint used by Manager 2.0.
            for item in ITEMS:
                live=manager.recipe(item,'Kurunegala')['sheet_draft']
                assert live['current']['ingredients']==corrected(item,before[item][-1]['ingredients'])
                if item!='KG-CHIPS-SPICY':
                    assert not any('measured quantity' in note for note in live['pending_notes'])
            print(frappe.as_json(dict(updated=changed,combined_sauce=COMBINED,history_preserved=True)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
