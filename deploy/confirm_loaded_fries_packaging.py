"""Record the owner's per-portion packaging without inventing opening stock."""
from copy import deepcopy

ITEMS=('KG-FRIES-CHICKEN','KG-FRIES-FISH','KG-FRIES-PRAWN','KG-FRIES-BEEF','KG-FRIES-SAUSAGE')
PACKAGING=(
    ('Loaded fries box',1,{'loaded fries box','loaded fries boxes'}),
    ('Disposable spoon',2,{'disposable spoon','disposable spoons','spoon','spoons'}),
    ('Wooden fork',2,{'wooden fork','wooden forks','disposable fork','disposable forks','fork','forks'}),
    ('Serviettes',2,{'serviettes','serviette','serviette paper','serviette papers','serviet paper','serviet papers'}),
)
NOTE='Packaging per loaded-fries portion: 1 box, 2 disposable spoons, 2 forks and 2 serviettes, counted individually in Nos.'


def packaging_rows(ingredients):
    names=set().union(*(row[2] for row in PACKAGING))
    return [r for r in ingredients if r['name'].strip().casefold() in names]


def corrected(ingredients):
    rows=deepcopy(ingredients)
    if any(r['name'].strip().casefold() in ('packaging','packaging set') for r in rows):
        raise ValueError('Review the combined packaging row before adding individual components.')
    for name,qty,aliases in PACKAGING:
        matches=[r for r in rows if r['name'].strip().casefold() in aliases]
        if len(matches)>1:raise ValueError('Review duplicate packaging rows: '+name)
        if matches:matches[0].update(name=name,qty=qty,unit='Nos')
        else:rows.append(dict(name=name,qty=qty,unit='Nos'))
    return rows


def draft_note(note):
    if NOTE in note:return note
    result=(note+'\n'+NOTE).strip()
    if len(result)>1000:raise ValueError('Shorten the draft note before recording packaging quantities.')
    return result


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
            before=deepcopy(stored(warehouse,lock=True))
            recipes={code:manager.recipe(code) for code in ITEMS}
            registered=[]
            for name,qty,aliases in PACKAGING:
                expected_code={'Wooden fork':'Wooden fork','Serviettes':'Serviettes'}.get(name)
                if expected_code:
                    assert frappe.db.get_value('Item',expected_code,'stock_uom')=='Nos'
                    balance=frappe.db.get_value('Bin',{'item_code':expected_code,'warehouse':warehouse},'actual_qty')
                item=manager.create_ingredient(name,'Nos',minimum_qty=0,restaurant='Kurunegala',item_code=expected_code)
                assert item['stock_uom']=='Nos'
                if expected_code:
                    assert frappe.db.get_value('Bin',{'item_code':expected_code,'warehouse':warehouse},'actual_qty')==balance
                registered.append(dict(item=item['item'],name=name,qty=qty,unit='Nos'))
            updated=[]
            for code in ITEMS:
                history=before[code];latest=history[-1]
                rows=corrected(latest['ingredients'])
                saved=manager.save_sheet_draft(code,rows,latest['revision'],draft_note(latest['note']),'Kurunegala')
                assert saved['history'][:len(history)]==history
                assert [r for r in rows if r not in packaging_rows(rows)]==[r for r in latest['ingredients'] if r not in packaging_rows(latest['ingredients'])]
                if len(saved['history'])>len(history):updated.append(dict(item=code,revision=saved['current']['revision']))
            after=stored(warehouse)
            for code,history in before.items():
                if code not in ITEMS:assert after[code]==history
            for code in ITEMS:
                current=manager.recipe(code)
                assert current['version']==recipes[code]['version'] and current['ingredients']==recipes[code]['ingredients']
                packaging=packaging_rows(current['sheet_draft']['current']['ingredients'])
                assert len(packaging)==4
                assert {r['name']:(r['qty'],r['unit']) for r in packaging}=={n:(q,'Nos') for n,q,a in PACKAGING}
            frappe.db.commit()
            print(frappe.as_json(dict(updated=updated,packaging_stock_items=registered,
                previous_history_preserved=True,stock_quantities_changed=False,stock_recipes_activated=False)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
