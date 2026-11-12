"""Local preview of safe unused-item unit edits; no connection to production."""
import json
from uuid import uuid4
UNITS=['Nos','Kg','g','L','Litre','ml','Pkt','Pack','Box','Dozen']

def describe(db,item):
    def references(value):
        if isinstance(value,dict):return any(references(v) for v in value.values())
        if isinstance(value,list):return any(references(v) for v in value)
        return value==item
    row=db.execute('SELECT * FROM stock WHERE item=?',(item,)).fetchone()
    if not row:raise ValueError('Choose an item in this branch inventory.')
    reasons=[]
    if row['balance']:reasons.append('Existing stock balance')
    if row['rate']:reasons.append('Saved item prices or valuation')
    if row['minimum_qty']:reasons.append('Minimum stock levels')
    for table,column,label in [('recipes','ingredients','Recipe ingredients'),('purchase_bills','payload','Purchase bills'),('movements','details','Stock movement history')]:
        rows=db.execute('SELECT * FROM '+table).fetchall()
        if any(references(json.loads(r[column])) for r in rows if table!='movements' or r['kind'] not in ('item_unit','item_name','ingredient')):
            reasons.append(label)
    history=[]
    for movement in db.execute("SELECT * FROM movements WHERE kind='item_unit' ORDER BY creation DESC"):
        data=json.loads(movement['details'])
        if data['item']==item:history.append(dict(before=data['before'],after=data['after'],by=movement['owner'],when=movement['creation']))
    return dict(item=item,unit=row['stock_uom'],units=[dict(name=u,must_be_whole_number=int(u in ('Nos','Box'))) for u in UNITS],can_change=not reasons,reasons=reasons,history=history[:5])

def change(db,p):
    from server import now
    item=p['item_code'];info=describe(db,item);unit=p.get('unit')
    if unit is None or unit==info['unit']:return False
    if p.get('expected_unit')!=info['unit']:raise ValueError('This item unit changed. Reopen the item before saving.')
    if unit not in UNITS:raise ValueError('Choose an enabled, defined unit')
    if info['reasons']:raise ValueError('The stock unit is locked: '+', '.join(info['reasons']))
    display={'Kg':'g','L':'ml','Litre':'ml'}.get(unit,unit)
    db.execute('UPDATE stock SET stock_uom=?,uom=? WHERE item=?',(unit,display,item))
    db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',('ITEM-UNIT-'+uuid4().hex,now(),'Local manager','item_unit',json.dumps(dict(item=item,before=info['unit'],after=unit))))
    return True
