"""Local-only approved category corrections, with immutable receipt/correction history."""
from collections import defaultdict
import hashlib
import json


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS purchase_category_corrections(
        request_id TEXT PRIMARY KEY, bill TEXT NOT NULL, created TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_category_correction_update BEFORE UPDATE ON purchase_category_corrections
        BEGIN SELECT RAISE(ABORT,'Category correction history is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_category_correction_delete BEFORE DELETE ON purchase_category_corrections
        BEGIN SELECT RAISE(ABORT,'Category correction history is immutable'); END;''')


def history(db, name):
    return [dict(json.loads(r['details']), request_id=r['request_id'], by=r['actor'], when=r['created'])
            for r in db.execute('SELECT * FROM purchase_category_corrections WHERE bill=? ORDER BY rowid DESC', (name,))]


def review(db, name, actor):
    from existing_purchases import bill_dict, CATEGORIES
    if actor != 'Local manager': raise ValueError('Only a manager can do this.')
    row = db.execute('SELECT * FROM purchase_bills WHERE name=?', (name,)).fetchone()
    if not row or row['status'] != 'approved': raise ValueError('Only approved bills can use this correction.')
    bill = bill_dict(row, db)
    past = history(db, name)
    balances = {}
    for line in bill['lines']:
        stock = db.execute('SELECT * FROM stock WHERE item=?', (line.get('item'),)).fetchone()
        line.update(can_change=True, can_receive=bool(stock and stock['stock_uom']==line['unit']),
                    stock_qty=line['qty'], stock_unit=stock['stock_uom'] if stock else line['unit'],
                    received_qty=(past[0]['allocations'].get(line['row_name'],0) if past else
                                  line['qty'] if row['stock_entry'] and line['category']=='Food / Ingredients' else 0))
        if stock: balances[line['item']] = stock['balance']
    return dict(bill=bill, history=past, warehouse='Local review stock', balances=balances,
                categories=[dict(value=c['name'],label=c['name'],in_stock=c['in_stock']) for c in CATEGORIES],
                revision=(row['resolved_at'] or '')+'|'+(past[0]['request_id'] if past else ''))


def correct(db, p, actor):
    from server import now
    data = review(db, p.get('name'), actor)
    changes = p.get('line_categories') or []
    if isinstance(changes, str): changes = json.loads(changes)
    if not isinstance(changes,list): raise ValueError('Choose valid item categories.')
    token = p.get('request_id') or ''
    if not 16<=len(token)<=80: raise ValueError('Reload this correction.')
    digest = hashlib.sha256(json.dumps([p['name'],changes,p.get('comment','')],sort_keys=True).encode()).hexdigest()
    existing = db.execute('SELECT details FROM purchase_category_corrections WHERE request_id=?',(token,)).fetchone()
    if existing:
        if json.loads(existing['details'])['payload_hash']!=digest: raise ValueError('Correction request already used.')
        return dict(ok=True,already_processed=True,**data)
    if p.get('expected_revision')!=data['revision']: raise ValueError('This bill changed on another screen. Reopen it.')
    rows={r['row_name']:r for r in data['bill']['lines']}
    options={c['value']:c for c in data['categories']}
    allocations={key:r['received_qty'] for key,r in rows.items()}
    changes_saved=[];seen=set();incoming=defaultdict(float);outgoing=defaultdict(float);cost=defaultdict(float)
    for change in changes:
        key=change.get('row_name') if isinstance(change,dict) else None
        if key not in rows or key in seen:raise ValueError('A bill line is missing, duplicated or belongs to another bill.')
        seen.add(key);line=rows[key];category=options.get(change.get('category'))
        if not category:raise ValueError('Pick a valid category for every item.')
        old=line['category']
        if category['value']==old:continue
        if category['in_stock'] and not line['can_receive']:raise ValueError('A linked stock item is required.')
        before=allocations[key]
        after=(before or (0 if line['is_stock'] else line['stock_qty'])) if category['in_stock'] else 0
        delta=after-before;allocations[key]=after
        if delta>0:incoming[line['item']]+=delta;cost[line['item']]+=delta*line['price']
        elif delta<0:outgoing[line['item']]-=delta
        changes_saved.append(dict(row_name=key,name=line['name'],before=old,after=category['value'],
                                  stock_delta=delta,unit=line['stock_unit']))
        line['category']=category['value']
    if not changes_saved:return dict(ok=True,changed=False,**data)
    movements=[]
    for item in sorted(set(incoming)|set(outgoing)):
        stock=db.execute('SELECT * FROM stock WHERE item=?',(item,)).fetchone()
        if outgoing[item] and stock['balance']-outgoing[item]<-1e-7:raise ValueError('Not enough stock to make this correction.')
        remaining=stock['balance']-outgoing[item];after=remaining+incoming[item];rate=stock['rate']
        if incoming[item]:rate=cost[item]/incoming[item] if remaining<=0 else (remaining*rate+cost[item])/after
        db.execute('UPDATE stock SET balance=?,rate=? WHERE item=?',(after,rate,item))
        movements.append(dict(item=item,before=stock['balance'],after=after,delta=after-stock['balance']))
    entry='LOCAL-CATEGORY-'+token
    if movements:db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(entry,now(),actor,'purchase_category_correction',json.dumps(dict(bill=p['name'],lines=movements))))
    record=dict(payload_hash=digest,comment=p.get('comment',''),changes=changes_saved,allocations=allocations,
                bill=data['bill'],new_stock_entries=[entry] if movements else [],movements=movements)
    db.execute('INSERT INTO purchase_category_corrections VALUES(?,?,?,?,?)',(token,p['name'],now(),actor,json.dumps(record)))
    import purchase_defaults
    purchase_defaults.remember(db,changes_saved,data['bill']['lines'])
    return dict(ok=True,changed=True,**review(db,p['name'],actor))
