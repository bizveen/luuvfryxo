"""Record the owner's estimated oil allocation in seven menu recipe drafts."""
from copy import deepcopy

ITEMS=('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN','KG-FRIES-BEEF',
       'KG-FRIES-SAUSAGE','KG-CHIPS-FISH','KG-CHIPS-SPICY')
OIL_NAMES={'frying oil','fry oil','cooking oil','oil'}
NOTE='Estimated frying oil consumption: 50 ml per finished menu portion. Verify the actual oil balance during stock audits.'


def oil_rows(ingredients):
    return [row for row in ingredients if row['name'].strip().casefold() in OIL_NAMES]


def corrected(ingredients):
    rows=deepcopy(ingredients);matches=oil_rows(rows)
    if len(matches)>1:raise ValueError('Multiple frying oil rows need review before applying the estimate.')
    if matches:matches[0].update(qty=50,unit='ml')
    else:rows.append(dict(name='Frying oil',qty=50,unit='ml'))
    return rows


def draft_note(note):
    if NOTE in note:return note
    updated=(note+'\n'+NOTE).strip()
    if len(updated)>1000:raise ValueError('The draft note needs shortening before recording the oil estimate.')
    return updated


def run_native():
    import frappe
    from unittest.mock import patch
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:
        from luuvcrm import branch
        from luuvcrm.api import manager_inventory as manager
        from luuvcrm.api.recipe_sheets import stored
        with patch.object(branch,'selected',return_value='Kurunegala'):
            warehouse=branch.warehouse('Kurunegala')
            before=deepcopy(stored(warehouse,lock=True));changed=[]
            for item in ITEMS:
                history=before[item];latest=history[-1]
                rows=corrected(latest['ingredients']);note=draft_note(latest.get('note',''))
                if rows==latest['ingredients'] and note==latest['note']:continue
                saved=manager.save_sheet_draft(item,rows,latest['revision'],note,restaurant='Kurunegala')
                assert saved['history'][:-1]==history
                assert saved['current']['ingredients']==rows
                assert [r for r in rows if r not in oil_rows(rows)]==[r for r in latest['ingredients'] if r not in oil_rows(latest['ingredients'])]
                changed.append(dict(item=item,revision=saved['current']['revision']))
            after=stored(warehouse)
            for item in before:
                if item not in {row['item'] for row in changed}:assert after[item]==before[item]
            frappe.db.commit()
            for item in ITEMS:
                live=manager.recipe(item,'Kurunegala')['sheet_draft']['current']
                oils=oil_rows(live['ingredients'])
                assert len(oils)==1 and oils[0]['qty']==50 and oils[0]['unit']=='ml'
                assert NOTE in live['note']
            print(frappe.as_json(dict(updated=changed,recipes_verified=7,estimated_ml_per_portion=50,
                                     history_preserved=True,stock_activation_changed=False)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
