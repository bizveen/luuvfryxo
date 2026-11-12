"""Recipe-sheet drafts retain source quantities without inventing stock conversions."""
from copy import deepcopy
import json
import math
from pathlib import Path

UNITS = ('g','Kg','ml','L','Nos','pieces','portion','set')


def references(restaurant):
    data=json.loads((Path(__file__).parent/'data/kurunegala-recipes.json').read_text(encoding='utf-8'))
    if restaurant!=data['restaurant']:return {}
    result={r['menu_item']:r for r in data['recipes']}
    prepared=json.loads((Path(__file__).parent/'data/coleslaw-sample.json').read_text(encoding='utf-8'))
    result[prepared['menu_item']]=prepared
    return result


def initial(reference):
    return dict(revision=0,by='Sample preparation draft' if reference.get('sample') else 'Supplied recipe sheet',when='2026-11-12',note=reference['source_note'] if reference.get('sample') else 'Original recipe supplied by the owner.',
        ingredients=[dict(name=r['name'],qty=r['qty'],unit=r['unit']) for r in reference['ingredients']])


def view(reference,history=None):
    history=deepcopy(history or [initial(reference)])
    notes=['Confirm sauce stock units before linking this draft to stock.',
           'Confirm the prepared salad and any oil/flour used during preparation.',
           'Packaging is listed as one set; its stock components need linking.']
    if reference.get('kind')=='prepared':
        notes=['Confirm the actual ingredients and finished batch yield, then link each raw ingredient to its stock item.',
               'Complete a batch only after preparation. This consumes raw ingredients and adds finished coleslaw stock.']
    current=history[-1]['ingredients']
    portions=[r['name'] for r in current if r['unit']=='portion']
    if portions:
        notes.append('Enter a measured quantity for: '+', '.join(portions)+'.')
    blends=[r for r in current if '&' in r['name'] and 'sauce' in r['name'].lower()]
    for row in blends:
        notes.append('The '+str(row['qty'])+' '+row['unit']+' '+row['name']+' amount is combined. Confirm the split or prepared blend.')
    if reference.get('source_note'):notes.append(reference['source_note'])
    preparations=[dict(item='KG-STOCK-COLESLAW',name='Coleslaw salad',qty=row['qty'],unit=row['unit'])
        for row in current if row['name'].strip().casefold() in ('creamy coleslaw','coleslaw','coleslaw salad')]
    return dict(reference=deepcopy(reference),current=history[-1],history=history,pending_notes=notes,units=list(UNITS),preparations=preparations)


def changed(history,ingredients,note,expected_revision,by,when):
    if not isinstance(ingredients,list) or not 1<=len(ingredients)<=100:
        raise ValueError('Add between 1 and 100 ingredient rows.')
    clean=[]
    for row in ingredients:
        if not isinstance(row,dict):raise ValueError('Enter a valid ingredient row.')
        name=row.get('name');unit=row.get('unit');qty=row.get('qty')
        if not isinstance(name,str) or not 1<=len(name.strip())<=140 or any(ord(c)<32 or c in '<>' for c in name):
            raise ValueError('Use a plain-text ingredient name of 1–140 characters.')
        if unit not in UNITS:raise ValueError('Choose a listed recipe unit.')
        try:value=float(qty) if not isinstance(qty,bool) else float('nan')
        except (ValueError,TypeError):value=float('nan')
        if not math.isfinite(value) or value<=0:raise ValueError('Each ingredient needs a positive quantity.')
        if unit in ('Nos','pieces','set') and value!=int(value):raise ValueError('Enter a whole number of '+unit+'.')
        clean.append(dict(name=name.strip(),qty=value,unit=unit))
    if not isinstance(note,str) or len(note)>1000:raise ValueError('Keep the draft note under 1000 characters.')
    latest=history[-1]
    if clean==latest['ingredients'] and note.strip()==latest['note']:return None
    if str(expected_revision)!=str(latest['revision']):
        raise ValueError('This draft changed on another screen. Reopen the recipe before saving.')
    return dict(revision=latest['revision']+1,ingredients=clean,note=note.strip(),by=by,when=when)
