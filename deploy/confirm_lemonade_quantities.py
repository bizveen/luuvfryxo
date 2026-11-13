"""Append confirmed Sprite/Blueberry amounts; preserve all other draft details."""
from copy import deepcopy

OLD_NOTE='Shared base for Strawberry, Passion and Blueberry lemonade, as instructed by the owner. Use a 300 ml cup. Keep the previously requested juice bag and add one straw. Sprite is provisionally read as 280 ml; Blueberry is provisionally read as 20 g. Confirm these handwritten quantities before stock activation.'


def corrected(current, code, confirmed_note):
    rows=deepcopy(current['ingredients'])
    targets={'Sprite':(210,'ml')}
    if code=='KG-LEMONADE-BLUEBERRY':targets['Blueberry']=(20,'g')
    for name,(qty,unit) in targets.items():
        matches=[r for r in rows if r['name'].strip().casefold()==name.casefold()]
        if len(matches)!=1:raise ValueError('Review missing or duplicate '+name+' row.')
        if matches[0]['unit']!=unit:raise ValueError('Review '+name+' unit before applying the confirmed quantity.')
        matches[0]['qty']=qty
    note=current['note'].replace(OLD_NOTE,confirmed_note)
    if confirmed_note not in note:note=(note+'\n'+confirmed_note).strip()
    if len(note)>1000:raise ValueError('Shorten the draft note before recording the confirmation.')
    if 'provisionally' in note:raise ValueError('Review remaining provisional quantities in the current note.')
    return rows,note


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
            for code,ref in refs.items():
                current=recipes[code]['sheet_draft']['current']
                rows,note=corrected(current,code,ref['beverage_recipe']['source_note'])
                manager.save_sheet_draft(code,rows,current['revision'],note,'Kurunegala')
            after=stored(warehouse)
            for code,history in before.items():
                if code in refs:assert after[code][:len(history)]==history
                else:assert after[code]==history
            for code,ref in refs.items():
                recipe=manager.recipe(code)
                assert recipe['version']==recipes[code]['version'] and recipe['ingredients']==recipes[code]['ingredients']
                rows,note=corrected(recipes[code]['sheet_draft']['current'],code,ref['beverage_recipe']['source_note'])
                assert recipe['sheet_draft']['current']['ingredients']==rows
                assert recipe['sheet_draft']['current']['note']==note
            frappe.db.commit()
            print(frappe.as_json(dict(confirmed_sprite_ml=210,confirmed_blueberry_g=20,
                draft_versions={code:after[code][-1]['revision'] for code in refs},history_preserved=True,stock_recipes_changed=False)))
    finally:frappe.db.rollback();frappe.destroy()


if __name__=='__main__':run_native()
