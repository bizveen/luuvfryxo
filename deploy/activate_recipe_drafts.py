"""Authorized provisional opening stock and branch recipe activation.

The runner supplies PLAN and APPLY. Preflight runs the same operations and rolls
back; only an explicit APPLY run commits. No purchase or payment is fabricated.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import json

TAG='KG-PROVISIONAL-WEEK-20261113'


def stock_quantity(qty, unit, stock_unit):
    if unit==stock_unit or (unit in ('Nos','pieces') and stock_unit=='Nos'):return float(qty)
    if unit=='g' and stock_unit in ('Kg','Kilogram'):return float(qty)/1000
    if unit=='ml' and stock_unit in ('L','Litre','Liter'):return float(qty)/1000
    raise ValueError('No measured conversion from '+unit+' to '+stock_unit)


def rounded(qty, unit):
    qty=round(float(qty),9)
    step=Decimal('1') if unit=='Nos' else Decimal('10') if unit=='g' else Decimal('0.01') if qty<1 else Decimal('0.1')
    return float((Decimal(str(qty))/step).to_integral_value(rounding=ROUND_CEILING)*step)


def run(plan, apply=False):
    assert plan.get('exact_plan_approved') is True, 'The exact stock/mapping/valuation plan is awaiting owner approval.'
    review=plan['reviewed_proposal']
    assert review['period']==[plan['period_start'],plan['period_end_exclusive']]
    import frappe
    from unittest.mock import patch
    from frappe.utils import flt
    from luuvcrm import activity,branch,recipe_sheets
    from luuvcrm.api import manager_inventory as manager,inventory,manager_preparation
    from luuvcrm.api.recipe_sheets import stored
    restaurant=plan['restaurant'];warehouse=branch.warehouse(restaurant)
    company=frappe.db.get_value('Warehouse',warehouse,'company')
    report={'applied':apply,'tag':TAG,'period':[plan['period_start'],plan['period_end_exclusive']],
            'stock':[],'recipes':[],'pending':plan['pending'],'unpriced':[]}
    with patch.object(branch,'selected',return_value=restaurant):
        frappe.db.sql('SELECT name FROM tabWarehouse WHERE name=%s FOR UPDATE',warehouse)
        assert not frappe.db.exists('Stock Reconciliation',{'luuv_audit_restaurant':restaurant,'docstatus':1,'luuv_audit_details':['like','%'+TAG+'%']}),'Opening stock was already applied; do not replenish a second time.'
        before_sheets=deepcopy(stored(warehouse,lock=True))
        refs=recipe_sheets.references(restaurant)
        original_defaults={code:frappe.db.get_value('Item',code,'default_bom') for code in refs}
        originals={code:manager.recipe(code) for code in refs}
        # Append the owner's confirmed base and 10 g mint without rewriting history.
        lemon=originals['KG-LEMON-MINT']['sheet_draft']['current']
        rows=deepcopy(lemon['ingredients'])
        if not any(r['name']=='Sprite' for r in rows):
            rows=[dict(name='Ice',qty=50,unit='g'),dict(name='Sprite',qty=210,unit='ml'),dict(name='Lime',qty=20,unit='g')]+rows
        if not any(r['name']=='Straw' for r in rows):rows.append(dict(name='Straw',qty=1,unit='Nos'))
        if not any(r['name']=='Mint' for r in rows):rows.append(dict(name='Mint',qty=10,unit='g'))
        if rows!=lemon['ingredients']:
            manager.save_sheet_draft('KG-LEMON-MINT',rows,lemon['revision'],
                'Owner confirmed shared lemonade base and 10 g mint per drink on 2026-11-13.',restaurant)
        recipes={code:manager.recipe(code) for code in refs}
        expected_recipes={row['item']:row['rows'] for row in review['recipes']}
        assert set(recipes)==set(expected_recipes), 'Recipe list changed after the plan was prepared.'
        for code,recipe in recipes.items():
            rows=[{key:row[key] for key in ('name','qty','unit')} for row in recipe['sheet_draft']['current']['ingredients']]
            assert rows==expected_recipes[code], (code,'Recipe changed after the plan was prepared.')
        counts=review['menu_portions']
        report['portions']=counts
        mapping={};spec_by_code={}
        for label,spec in plan['items'].items():
            code=spec.get('existing')
            if code:
                doc=frappe.get_doc('Item',code,for_update=True)
                assert doc.stock_uom==spec['unit'] and not doc.disabled and not doc.is_fixed_asset,(label,'item configuration changed')
                if not doc.is_stock_item:
                    assert not frappe.db.exists('Stock Ledger Entry',{'item_code':code}),(code,'unexpected stock history')
                    doc.is_stock_item=1;doc.save(ignore_permissions=True)
                    activity.log_activity('item.stock_tracking',label='Enabled stock tracking: '+doc.item_name,
                        ref_doctype='Item',ref_name=code,item=code,detail={'restaurant':restaurant,'reason':TAG,'stock_uom_unchanged':spec['unit']})
                manager.create_ingredient(doc.item_name,spec['unit'],restaurant=restaurant,item_code=code)
            else:
                result=manager.create_ingredient(spec['create'],spec['unit'],restaurant=restaurant)
                assert result['stock_uom']==spec['unit']
                code=result['item']
            mapping[label]=code;spec_by_code[code]=spec
        catalog={r['item']:r for r in manager._catalog(warehouse)}
        demand=defaultdict(float);bom_rows={}
        for code,recipe in recipes.items():
            merged=defaultdict(float)
            for row in recipe['sheet_draft']['current']['ingredients']:
                ingredient=mapping[row['name']]
                quantity=stock_quantity(row['qty'],row['unit'],catalog[ingredient]['stock_uom'])
                merged[ingredient]+=quantity
                if recipe['kind']!='prepared':demand[ingredient]+=quantity*counts.get(code,0)
            bom_rows[code]=[dict(item=item,qty=qty*catalog[item]['factor']) for item,qty in merged.items()]
        # Carry one day of estimated finished salad plus six days of raw inputs.
        # This is opening stock, not a claim that a preparation batch took place.
        prepared='KG-STOCK-COLESLAW';salad_week=demand[prepared]
        salad_finished=rounded(salad_week/7,'Kg')
        demand[prepared]=salad_finished
        remainder=max(0,salad_week-salad_finished)
        for row in recipes[prepared]['sheet_draft']['current']['ingredients']:
            ingredient=mapping[row['name']]
            demand[ingredient]+=stock_quantity(row['qty'],row['unit'],catalog[ingredient]['stock_uom'])*remainder
        expected_stock={row['item']:row for row in review['stock']}
        assert len(demand)==len(expected_stock), 'Stock plan item count changed.'
        for code in sorted(demand):
            info=catalog[code];spec=spec_by_code[code]
            key=spec.get('existing') or 'NEW:'+spec['create']
            expected=expected_stock[key]
            before=flt(info['balance'])
            assert info['stock_uom']==expected['unit'] and abs(before-expected['current'])<1e-6, (code,'Stock changed; review the plan again.')
            assert abs(demand[code]-expected['weekly_need'])<1e-6, (code,'Demand differs from reviewed plan.')
            target=flt(expected['target']);rate=flt(expected['rate']);rate_source=expected['rate_source']
            assert target>=before and rate>=0
            if rate<=0:report['unpriced'].append(info['name'])
            comment=(TAG+' | PROVISIONAL opening quantity authorized by owner; NOT a physical count or purchase receipt. '
                +'Based on Kurunegala paid menu portions, 2026-11-06 to 2026-11-12. Correct actual quantity at next audit. '
                +'Salad stock: 1 day finished + remaining week raw materials. Valuation: '+rate_source+'.')
            details=dict(item=code,name=info['name'],uom=info['stock_uom'],before=before,counted=target,
                difference=target-before,comment=comment,provisional=True,plan=TAG,week_demand=demand[code],valuation_source=rate_source)
            entry=None
            if target>before+1e-8:
                doc=frappe.get_doc(dict(doctype='Stock Reconciliation',purpose='Stock Reconciliation',company=company,
                    posting_date=frappe.utils.nowdate(),posting_time=frappe.utils.nowtime(),set_posting_time=1,
                    expense_account=frappe.db.get_value('Company',company,'stock_adjustment_account'),cost_center=branch.cost_center(restaurant),
                    luuv_audit_restaurant=restaurant,luuv_audit_details=frappe.as_json(details),
                    items=[dict(item_code=code,warehouse=warehouse,qty=target,valuation_rate=rate,allow_zero_valuation_rate=int(not rate))]))
                doc.insert(ignore_permissions=True);doc.flags.ignore_permissions=True;doc.submit();doc.add_comment('Comment',comment)
                entry=doc.name
                activity.log_activity('stock.audit',label='Provisional opening stock: '+info['name'],ref_doctype='Stock Reconciliation',
                    ref_name=entry,item=code,qty=target-before,detail=details)
            report['stock'].append(dict(item=code,name=info['name'],unit=info['stock_uom'],before=before,target=target,entry=entry,rate=rate,rate_source=rate_source))
        # The prepared recipe must exist before the menu recipe can consume its output.
        selected=[prepared]+[c for c in refs if c!=prepared and c not in plan['pending']]
        for code in selected:
            current=manager.recipe(code)
            note=TAG+' | Owner approved current recipe draft '+str(recipes[code]['sheet_draft']['current']['revision'])+'.'
            if code==prepared:note+=' Current 1000 g coleslaw sample approved for provisional live use; confirm actual yield at next audit.'
            saved=manager.save_recipe(code,bom_rows[code],current['latest'],restaurant,
                output_qty=refs[code].get('output_qty') if code==prepared else None,recipe_note=note)
            active=manager.activate_recipe(code,saved['latest'],restaurant)
            assert active['status']=='Active'
            report['recipes'].append(dict(item=code,version=active['version'],ingredients=len(active['ingredients'])))
        after_sheets=stored(warehouse)
        for code,history in before_sheets.items():assert after_sheets[code][:len(history)]==history
        # A real stock issue in a savepoint proves posting works, including unpriced stock.
        frappe.db.savepoint('recipe_issue_check')
        for menu in ['KG-FRIES-CHICKEN','KG-LEMONADE-STRAWBERRY','KG-LEMON-MINT']:
            version=next(r['version'] for r in report['recipes'] if r['item']==menu)
            _,rows=inventory._components(menu,1,company,recipe_bom=version,resolve_default=False)
            balances={r['item_code']:flt(frappe.db.get_value('Bin',{'item_code':r['item_code'],'warehouse':warehouse},'actual_qty')) for r in rows}
            entry=inventory._new_stock_entry('Material Issue',company,warehouse,'Rollback-only recipe activation validation')
            for row in rows:entry.append('items',dict(row,s_warehouse=warehouse,cost_center=branch.cost_center(restaurant)))
            inventory._submit(entry)
            for row in rows:
                actual=flt(frappe.db.get_value('Bin',{'item_code':row['item_code'],'warehouse':warehouse},'actual_qty'))
                assert abs(actual-(balances[row['item_code']]-row['qty']))<1e-6,(menu,row['item_code'],'Incorrect deduction')
        prepared_version=next(r['version'] for r in report['recipes'] if r['item']==prepared)
        batch=manager_preparation.complete(prepared,prepared_version,TAG+'-rollback-check',1000,1000,
            'Rollback-only preparation validation',restaurant)
        retry=manager_preparation.complete(prepared,prepared_version,TAG+'-rollback-check',1000,1000,
            'Rollback-only preparation validation',restaurant)
        assert retry['already_processed'] and retry['name']==batch['name']
        for row in batch['ingredients']:
            actual=flt(frappe.db.get_value('Bin',{'item_code':row['item'],'warehouse':warehouse},'actual_qty'))
            assert abs(actual-row['after'])<1e-6,(row['item'],'Incorrect preparation consumption')
        actual=flt(frappe.db.get_value('Bin',{'item_code':prepared,'warehouse':warehouse},'actual_qty'))
        assert abs(batch['received_qty']-1)<1e-6 and abs(actual-batch['after'])<1e-6
        frappe.db.rollback(save_point='recipe_issue_check')
        for row in report['stock']:
            actual=frappe.db.get_value('Bin',{'item_code':row['item'],'warehouse':warehouse},'actual_qty') or 0
            assert abs(actual-row['target'])<1e-6,(row['name'],actual,row['target'])
        assert {code:frappe.db.get_value('Item',code,'default_bom') for code in refs}==original_defaults
        report['deduction_check']='Three exact stock issues and one idempotent 1 Kg coleslaw preparation passed, then rolled back; no test usage retained.'
        if apply:frappe.db.commit()
        else:frappe.db.rollback()
        return report


if __name__=='__main__':
    import frappe
    frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
    try:print(frappe.as_json(run(PLAN,APPLY)))
    finally:frappe.db.rollback();frappe.destroy()
