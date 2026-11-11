"""Explicit user-requested sample preparation, in the local review database only.

Creates one sample salad recipe, one finished batch, and one test sale. The
report prevents accidental reruns. Routine tests must use test_preparation.py.
"""
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen

BASE='http://127.0.0.1:4192/api/method/luuvcrm.api.manager_inventory.'
REPORT=Path(__file__).parent/'data/sample-preparation-verification.json'


def call(method,**params):
    request=Request(BASE+method,data=urlencode(params).encode(),method='POST')
    with urlopen(request,timeout=15) as response:
        return json.load(response)['message']


def run():
    if REPORT.exists():
        print('Sample preparation already verified. See '+str(REPORT));return
    old_recipe=call('recipe')
    old_orders=call('orders')
    salad=call('recipe',item_code='KG-STOCK-SALAD')
    if salad['version']:
        raise RuntimeError('A salad recipe already exists. Do not overwrite it with the sample.')
    raw=[]
    for name,qty,opening,rate,minimum in [('Cabbage (sample)',650,5,200,1),('Carrot (sample)',250,2,300,.5),('Onion (sample)',100,1,250,.25)]:
        item=call('create_ingredient',item_name=name,uom='Kg',minimum_qty=minimum)
        call('correct_stock',item_code=item['item'],expected_qty=0,counted_qty=opening,valuation_rate=rate,comment='Sample vegetables and example cost for localhost preparation testing only.')
        raw.append(dict(item=item['item'],qty=qty))
    recipe=call('save_recipe',item_code=salad['item'],ingredients=json.dumps(raw),output_qty=1000,expected_version='',recipe_note='SAMPLE ONLY: cabbage 650 g, carrot 250 g, onion 100 g makes 1,000 g salad. Replace with the actual Kurunegala recipe before real use.')
    call('activate_recipe',item_code=recipe['item'],version=recipe['version'])
    before={r['item']:r for r in call('dashboard')['stock']}
    params=dict(item_code=recipe['item'],version=recipe['version'],request_id='sample-salad-preparation-20261111',planned_qty=1000,actual_qty=1000,comment='User-requested sample salad batch; localhost test data only.')
    batch=call('complete_batch',**params)
    prepared={r['item']:r for r in call('dashboard')['stock']}
    assert abs(prepared[salad['item']]['balance']-before[salad['item']]['balance']-1)<1e-6
    for row in raw:assert abs(prepared[row['item']]['balance']-before[row['item']]['balance']+row['qty']/1000)<1e-6
    duplicate=call('complete_batch',**params);assert duplicate['already_processed']
    assert call('dashboard')['stock']==list(prepared.values())
    order=call('create_order',qty=1)
    sale=call('fire_order',order=order['name'])
    after={r['item']:r for r in call('dashboard')['stock']}
    assert all(after[r['item']]['balance']==prepared[r['item']]['balance'] for r in raw)
    saved_menu=call('recipe',item_code=old_recipe['item'],version=order['version'])
    for row in saved_menu['ingredients']:
        expected=row['qty']/after[row['item']]['factor']
        assert abs(prepared[row['item']]['balance']-after[row['item']]['balance']-expected)<1e-6
    assert call('fire_order',order=order['name'])['already_processed']
    assert call('dashboard')['stock']==list(after.values())
    latest_menu=call('recipe')
    assert latest_menu['version']==old_recipe['version'] and latest_menu['ingredients']==old_recipe['ingredients']
    orders_after=call('orders')
    for old in old_orders:assert next(r for r in orders_after if r['name']==old['name'])==old
    report=dict(local_only=True,sample_recipe=True,recipe=recipe['version'],batch=batch['name'],order=order['name'],preserved_existing_orders=len(old_orders),
        stock=[dict(item=r['name'],unit=r['stock_uom'],before=r['balance'],after_preparation=prepared[code]['balance'],after_sale=after[code]['balance']) for code,r in before.items() if code==salad['item'] or code in [i['item'] for i in raw]],
        raw_vegetables_deducted_again_on_sale=False,duplicate_batch_added_stock=False,duplicate_sale_deducted_stock=False)
    REPORT.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':run()
