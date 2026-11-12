"""Verify live recipe details and persist/reload an edit inside a rolled-back transaction."""
from copy import deepcopy
from unittest.mock import patch
import frappe
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity,recipe_sheets
    from luuvcrm.api import manager_inventory as manager
    from luuvcrm.api.recipe_sheets import stored
    with patch.object(branch,'selected',return_value='Kurunegala'),patch.object(activity,'log_activity'),patch.object(frappe,'enqueue'),patch.object(frappe,'publish_realtime'):
        codes=list(recipe_sheets.references('Kurunegala'));warehouse=branch.warehouse('Kurunegala')
        before=stored(warehouse)
        menu=manager.dashboard()['menu'];assert len([r for r in menu if r.get('has_sheet_draft')])==7
        for code in codes:
            result=manager.recipe(code);assert result['sheet_draft']['history']==before[code]
        recipe=manager.recipe('KG-FRIES-CHICKEN');sheet=recipe['sheet_draft'];rows=deepcopy(sheet['current']['ingredients'])
        rows[0]['qty']+=1
        result=manager.save_sheet_draft('KG-FRIES-CHICKEN',rows,sheet['current']['revision'],note='Rollback release verification')
        loaded=manager.recipe('KG-FRIES-CHICKEN');assert loaded['sheet_draft']==result
        assert loaded['sheet_draft']['history'][:-1]==sheet['history']
        assert loaded['version']==recipe['version'] and loaded['ingredients']==recipe['ingredients']
        frappe.db.rollback();assert stored(warehouse)==before
        print('PASS live Manager 2.0: seven recipe drafts visible; native database save/reload and earlier history verified; test edit rolled back.')
finally:frappe.db.rollback();frappe.destroy()
