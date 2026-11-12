"""Seed counted juice packaging drafts; preserve stock and existing recipes."""
from copy import deepcopy

ALIASES={
    'Clear cups':{'clear cups','clear cup','juice cup','juice cups','cup','cups'},
    'Juice lid':{'juice lid','juice lids','lid','lids'},
    'Juice bag':{'juice bag','juice bags'},
}
NOTE='Packaging per juice: 1 cup, 1 lid and 1 juice bag, counted individually in Nos.'


def corrected(ingredients):
    rows=deepcopy(ingredients)
    if any(r['name'].strip().casefold() in ('packaging','packaging set') for r in rows):
        raise ValueError('Review the combined packaging row before adding its components.')
    for name,aliases in ALIASES.items():
        matches=[r for r in rows if r['name'].strip().casefold() in aliases]
        if len(matches)>1:raise ValueError('Review duplicate packaging rows: '+name)
        if matches:matches[0].update(name=name,qty=1,unit='Nos')
        else:rows.append(dict(name=name,qty=1,unit='Nos'))
    return rows


def draft_note(note):
    if NOTE in note:return note
    result=(note+'\n'+NOTE).strip()
    if len(result)>1000:raise ValueError('Shorten the draft note before adding juice packaging.')
    return result


def run_native():
    import frappe
    from unittest.mock import patch
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:
        from luuvcrm import branch,recipe_sheets
        from luuvcrm.api import manager_inventory as manager
        from luuvcrm.api.recipe_sheets import FIELD,stored
        with patch.object(branch,'selected',return_value='Kurunegala'):
            warehouse=branch.warehouse('Kurunegala')
            refs={code:ref for code,ref in recipe_sheets.references('Kurunegala').items() if ref.get('packaging_only')}
            assert refs and set(refs)<=set(manager._menu('Kurunegala'))
            before=deepcopy(stored(warehouse,lock=True))
            stock_before=frappe.db.get_value('Bin',{'item_code':'Clear cups','warehouse':warehouse},'actual_qty')
            recipes={code:manager.recipe(code) for code in refs}
            registered=[]
            for name in ALIASES:
                code='Clear cups' if name=='Clear cups' else None
                item=manager.create_ingredient(name,'Nos',minimum_qty=0,restaurant='Kurunegala',item_code=code)
                assert item['stock_uom']=='Nos'
                registered.append(dict(item=item['item'],name=name,qty=1,unit='Nos'))
            values=deepcopy(before)
            for code,ref in refs.items():values.setdefault(code,[recipe_sheets.initial(ref)])
            frappe.db.set_value('Warehouse',warehouse,FIELD,frappe.as_json(values))
            for code in refs:
                latest=values[code][-1]
                saved=manager.save_sheet_draft(code,corrected(latest['ingredients']),latest['revision'],draft_note(latest['note']),'Kurunegala')
                assert saved['history'][:len(before.get(code,[]))]==before.get(code,[])
            after=stored(warehouse)
            for code,history in before.items():
                if code not in refs:assert after[code]==history
            for code in refs:
                current=manager.recipe(code)
                assert current['version']==recipes[code]['version'] and current['ingredients']==recipes[code]['ingredients']
                rows=current['sheet_draft']['current']['ingredients']
                assert {r['name']:(r['qty'],r['unit']) for r in rows if r['name'] in ALIASES}=={name:(1,'Nos') for name in ALIASES}
            assert frappe.db.get_value('Bin',{'item_code':'Clear cups','warehouse':warehouse},'actual_qty')==stock_before
            frappe.db.commit()
            print(frappe.as_json(dict(juice_drafts=list(refs),packaging_stock_items=registered,
                earlier_history_preserved=True,stock_quantities_changed=False,stock_recipes_activated=False)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
