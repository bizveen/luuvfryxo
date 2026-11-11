"""Local purchase requests and physical receipts. No supplier communication."""
import hashlib
import json
import math
from datetime import datetime,timezone


def now():return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def number(value,label,zero=False):
    try:n=float(value)
    except (TypeError,ValueError):n=float('nan')
    if not math.isfinite(n) or n<0 or (not zero and n==0):raise ValueError(label+' must be a valid '+('non-negative' if zero else 'positive')+' number.')
    return n


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS purchase_orders(name TEXT PRIMARY KEY,creation TEXT NOT NULL,request_id TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,vendor TEXT NOT NULL,source TEXT NOT NULL,note TEXT NOT NULL,items TEXT NOT NULL,cancelled_at TEXT,cancel_note TEXT);
        CREATE TABLE IF NOT EXISTS purchase_receipts(name TEXT PRIMARY KEY,creation TEXT NOT NULL,request_id TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,purchase TEXT NOT NULL,details TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS immutable_purchase BEFORE UPDATE OF name,creation,request_id,payload_hash,vendor,source,note,items ON purchase_orders BEGIN SELECT RAISE(ABORT,'Saved purchase requests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_receipt BEFORE UPDATE ON purchase_receipts BEGIN SELECT RAISE(ABORT,'Purchase receipts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_receipt_delete BEFORE DELETE ON purchase_receipts BEGIN SELECT RAISE(ABORT,'Purchase receipts are immutable'); END;
    ''')


def order_dict(db,row):
    order=dict(row);order.pop('payload_hash');order.pop('request_id')
    order['items']=json.loads(order['items']);order['receipts']=[]
    received={}
    for record in db.execute('SELECT * FROM purchase_receipts WHERE purchase=? ORDER BY creation DESC',(order['name'],)):
        receipt=dict(name=record['name'],creation=record['creation'],details=json.loads(record['details']))
        order['receipts'].append(receipt)
        for line in receipt['details']['items']:received[line['item']]=received.get(line['item'],0)+line['qty']
    for line in order['items']:
        line['received_qty']=round(received.get(line['item'],0),6)
        line['remaining_qty']=round(max(0,line['qty']-line['received_qty']),6)
    order['status']='cancelled' if order['cancelled_at'] else 'received' if all(i['remaining_qty']==0 for i in order['items']) else 'partial' if received else 'pending'
    return order


def orders(db):return [order_dict(db,r) for r in db.execute('SELECT * FROM purchase_orders ORDER BY creation DESC')]


def pending_quantities(db):
    pending={}
    for order in orders(db):
        if order['status'] in ('pending','partial'):
            for line in order['items']:pending[line['item']]=round(pending.get(line['item'],0)+line['remaining_qty'],6)
    return pending


def key_and_hash(p,payload):
    key=(p.get('request_id') or '').strip()
    if not 8<=len(key)<=100:raise ValueError('Reopen the form before saving.')
    return key,hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def handle(db,method,p,catalog):
    catalog={r['item']:r for r in catalog}
    if method=='purchases':return dict(orders=orders(db))
    if method=='save_purchase':
        vendor=(p.get('vendor') or '').strip();source=p.get('source') or 'supplier';note=(p.get('note') or '').strip()
        if not 2<=len(vendor)<=120:raise ValueError('Enter the supplier or Central Kitchen name (2–120 characters).')
        if source not in ('supplier','central_kitchen'):raise ValueError('Choose Supplier or Central Kitchen.')
        if len(note)>1000:raise ValueError('Keep the purchase note under 1000 characters.')
        rows=json.loads(p['items']) if isinstance(p.get('items'),str) else p.get('items')
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('Add between 1 and 100 purchase items.')
        lines=[];seen=set()
        for r in rows:
            if not isinstance(r,dict) or r.get('item') not in catalog:raise ValueError('Choose a stock item.')
            stock=catalog[r['item']]
            if stock['prepared']:raise ValueError(stock['name']+' is made in this branch. Use its preparation recipe.')
            if r['item'] in seen:raise ValueError('Combine duplicate purchase items into one quantity.')
            seen.add(r['item']);qty=number(r.get('qty'),'Order quantity')
            if stock['stock_uom']=='nos' and qty!=int(qty):raise ValueError('Enter a whole number of nos.')
            if round(qty,6)<=0:raise ValueError('Order quantity is too small.')
            lines.append(dict(item=r['item'],name=stock['name'],uom=stock['stock_uom'],qty=round(qty,6)))
        key,digest=key_and_hash(p,dict(vendor=vendor,source=source,note=note,items=lines))
        existing=db.execute('SELECT * FROM purchase_orders WHERE request_id=?',(key,)).fetchone()
        if existing:
            if existing['payload_hash']!=digest:raise ValueError('This request was already saved with different values. Reopen the form.')
            return dict(order_dict(db,existing),already_processed=True)
        name='LOCAL-PURCHASE-'+str(db.execute('SELECT count(*) FROM purchase_orders').fetchone()[0]+1).zfill(4)
        db.execute('INSERT INTO purchase_orders VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',(name,now(),key,digest,vendor,source,note,json.dumps(lines)))
        return order_dict(db,db.execute('SELECT * FROM purchase_orders WHERE name=?',(name,)).fetchone())
    record=db.execute('SELECT * FROM purchase_orders WHERE name=?',(p.get('purchase'),)).fetchone()
    if not record:raise ValueError('Purchase request not found.')
    order=order_dict(db,record)
    if method=='cancel_purchase':
        note=(p.get('comment') or '').strip()
        if not 3<=len(note)<=1000:raise ValueError('Add a reason for cancelling the remaining delivery.')
        if order['status']=='cancelled':return dict(order,already_processed=True)
        if order['status']=='received':raise ValueError('This purchase has already been received.')
        db.execute('UPDATE purchase_orders SET cancelled_at=?,cancel_note=? WHERE name=?',(now(),note,order['name']))
        return order_dict(db,db.execute('SELECT * FROM purchase_orders WHERE name=?',(order['name'],)).fetchone())
    if method!='receive_purchase':raise ValueError('Unknown purchasing operation.')
    bill_ref=(p.get('bill_ref') or '').strip();note=(p.get('note') or '').strip()
    if not bill_ref or len(bill_ref)>120:raise ValueError('Enter the bill or delivery reference (up to 120 characters).')
    if len(note)>1000:raise ValueError('Keep the receiving note under 1000 characters.')
    rows=json.loads(p['items']) if isinstance(p.get('items'),str) else p.get('items')
    if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('Enter the quantities actually received.')
    originals={r['item']:r for r in order['items']};lines=[];seen=set()
    for r in rows:
        if not isinstance(r,dict) or r.get('item') not in originals:raise ValueError('Receive only items on this request.')
        item=r['item'];stock=catalog[item]
        if item in seen:raise ValueError('Combine duplicate received items.')
        seen.add(item);qty=number(r.get('qty'),'Received quantity');rate=number(r.get('rate'),'Actual cost per stock unit',True)
        if stock['stock_uom']!=originals[item]['uom']:raise ValueError('The stock unit changed. Check this purchase before receiving.')
        if stock['stock_uom']=='nos' and qty!=int(qty):raise ValueError('Enter a whole number of nos.')
        if round(qty,6)<=0 or not math.isfinite(qty*rate):raise ValueError('Check the received quantity and price.')
        lines.append(dict(item=item,name=originals[item]['name'],uom=originals[item]['uom'],qty=round(qty,6),rate=rate))
    key,digest=key_and_hash(p,dict(purchase=order['name'],bill_ref=bill_ref,note=note,items=lines))
    existing=db.execute('SELECT * FROM purchase_receipts WHERE request_id=?',(key,)).fetchone()
    if existing:
        if existing['payload_hash']!=digest:raise ValueError('This receipt was already saved with different values. Reopen the form.')
        return dict(json.loads(existing['details']),already_processed=True)
    if order['status'] not in ('pending','partial'):raise ValueError('This purchase has no remaining delivery to receive.')
    # Different browser retries with the same reference cannot receive twice.
    for receipt in order['receipts']:
        if receipt['details']['bill_ref'].casefold()==bill_ref.casefold():raise ValueError('That delivery reference has already been received. Use a new reference for a new delivery.')
    for line in lines:
        if line['qty']>originals[line['item']]['remaining_qty']+1e-9:raise ValueError('Received quantity exceeds the remaining order: '+line['name'])
        stock=catalog[line['item']];after=round(stock['balance']+line['qty'],6)
        rate=line['rate'] if stock['balance']<=0 else (stock['balance']*stock['rate']+line['qty']*line['rate'])/after
        line.update(before=stock['balance'],after=after)
        db.execute('UPDATE stock SET balance=?,rate=? WHERE item=?',(after,rate,line['item']))
    name='LOCAL-RECEIPT-'+str(db.execute('SELECT count(*) FROM purchase_receipts').fetchone()[0]+1).zfill(4);stamp=now()
    details=dict(name=name,purchase=order['name'],vendor=order['vendor'],source=order['source'],bill_ref=bill_ref,note=note,items=lines,total=round(sum(i['qty']*i['rate'] for i in lines),2),owner='Local manager')
    db.execute('INSERT INTO purchase_receipts VALUES(?,?,?,?,?,?)',(name,stamp,key,digest,order['name'],json.dumps(details)))
    db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(name,stamp,'Local manager','purchase',json.dumps(details)))
    return details
