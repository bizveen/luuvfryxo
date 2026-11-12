"""Append the owner's lemonade recipes without changing past drafts or stock."""
from copy import deepcopy


def updated_rows(current, reference):
    requested=reference['beverage_recipe']['ingredients']
    names={r['name'].strip().casefold() for r in requested}
    if any(r['name'].strip().casefold() in ('packaging','packaging set') for r in current):
        raise ValueError('Review combined packaging before adding individual components.')
    for name in names:
        if sum(r['name'].strip().casefold()==name for r in current)>1:
            raise ValueError('Review duplicate ingredient: '+name)
    extras=[r for r in current if r['name'].strip().casefold() not in names]
    return deepcopy(requested+extras)


def updated_note(current, reference):
    source=reference['beverage_recipe']['source_note']
    if source in current:return current
    # The old packaging instruction is retained in its earlier saved revision.
    if current==reference.get('source_note'):return source
    result=(current+'\n'+source).strip()
    if len(result)>1000:raise ValueError('Shorten the draft note before adding the recipe source.')
    return result


def run_native():
    import frappe
    from unittest.mock import patch
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:
        from luuvcrm import branch,recipe_sheets
        from luuvcrm.api import manager_inventory as manager
        from luuvcrm.api.recipe_sheets import stored
        with patch.object(branch,'selected',return_value='Kurunegala'):
            warehouse=branch.warehouse('Kurunegala')
            refs={code:ref for code,ref in recipe_sheets.references('Kurunegala').items() if ref.get('beverage')}
            assert set(refs)=={'KG-LEMONADE-STRAWBERRY','KG-LEMONADE-PASSION','KG-LEMONADE-BLUEBERRY'}
            before=deepcopy(stored(warehouse,lock=True))
            recipes={code:manager.recipe(code) for code in refs}
            straw_before=frappe.db.get_value('Bin',{'item_code':'Straw','warehouse':warehouse},'actual_qty')
            straw=manager.create_ingredient('Straw','Nos',restaurant='Kurunegala',item_code='Straw')
            assert straw['stock_uom']=='Nos'
            for code,ref in refs.items():
                latest=recipes[code]['sheet_draft']['current']
                manager.save_sheet_draft(code,updated_rows(latest['ingredients'],ref),latest['revision'],updated_note(latest['note'],ref),'Kurunegala')
            after=stored(warehouse)
            for code,history in before.items():
                if code in refs:assert after[code][:len(history)]==history
                else:assert after[code]==history
            for code,ref in refs.items():
                current=manager.recipe(code)
                assert current['version']==recipes[code]['version'] and current['ingredients']==recipes[code]['ingredients']
                assert current['sheet_draft']['current']['ingredients']==updated_rows(recipes[code]['sheet_draft']['current']['ingredients'],ref)
            assert frappe.db.get_value('Bin',{'item_code':'Straw','warehouse':warehouse},'actual_qty')==straw_before
            frappe.db.commit()
            print(frappe.as_json(dict(lemonade_drafts={code:after[code][-1]['revision'] for code in refs},
                shared_base=True,straw_unit='Nos',history_preserved=True,stock_quantities_changed=False,stock_recipes_activated=False)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
