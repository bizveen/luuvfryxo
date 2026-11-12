"""Check live branch gates and staged recipe-sheet APIs without schema/data writes."""
import json,sys
from copy import deepcopy
from unittest.mock import patch
sys.path.insert(0,'/tmp/luuv-recipe-sheets-20261112/stage')
import frappe,luuvcrm
assert '/tmp/luuv-recipe-sheets-20261112/' in luuvcrm.__file__
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
    from luuvcrm import branch,activity
    from luuvcrm.api import manager_inventory as manager,recipe_sheets as api
    def blocked(fn,text):
        try:fn()
        except (frappe.ValidationError,frappe.PermissionError) as error:
            assert text.lower() in str(error).lower(),str(error);frappe.clear_messages()
        else:raise AssertionError('Expected rejection: '+text)
    state={};original_column=frappe.db.has_column
    def columns(doctype,field):return True if (doctype,field)==('Warehouse',api.FIELD) else original_column(doctype,field)
    def store(doctype,name,field,value,*args,**kwargs):
        assert doctype=='Warehouse' and field==api.FIELD
        state.clear();state.update(json.loads(value))
    with patch.object(branch,'selected',return_value='Kurunegala'),patch.object(frappe.db,'commit'),patch.object(activity,'log_activity'),patch.object(frappe,'publish_realtime'),patch.object(frappe,'enqueue'),patch.object(api,'stored',side_effect=lambda *a,**k:deepcopy(state)),patch.object(frappe.db,'has_column',side_effect=columns),patch.object(frappe.db,'set_value',side_effect=store):
        menu=manager.dashboard()['menu'];assert len([r for r in menu if r.get('has_sheet_draft')])==7
        from luuvcrm.recipe_sheets import references
        for code in references('Kurunegala'):
            result=manager.recipe(code);assert result['sheet_draft'] and result['sheet_draft']['current']['revision']==0
        first=manager.recipe('KG-FRIES-CHICKEN');rows=deepcopy(first['sheet_draft']['current']['ingredients']);rows[0]['qty']=210
        request=dict(item_code='KG-FRIES-CHICKEN',ingredients=rows,expected_revision=0,note='Rollback staged review')
        after=manager.save_sheet_draft(**request);assert after['current']['revision']==1
        assert after['history'][0]['ingredients'][0]['qty']==200
        assert manager.save_sheet_draft(**request)['current']['revision']==1
        blocked(lambda:manager.save_sheet_draft(**dict(request,note='Stale edit')),'another screen')
        again=manager.recipe('KG-FRIES-CHICKEN');assert again['sheet_draft']['current']['ingredients'][0]['qty']==210
        assert again['ingredients']==first['ingredients'] and again['version']==first['version']
        blocked(lambda:manager.save_sheet_draft(**dict(request,restaurant='Other')),'Switch')
        blocked(lambda:manager.save_sheet_draft(**dict(request,item_code='not-in-menu')),'menu')
        frappe.set_user('Guest');blocked(lambda:manager.save_sheet_draft(**request),'manager');frappe.set_user('Administrator')
        print('PASS seven visible recipes, editable drafts, previous versions, retry/stale guards, existing BOM preservation and branch/role gates')
finally:frappe.db.rollback();frappe.destroy()
