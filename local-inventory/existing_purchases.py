"""Isolated adapter for the EXISTING /purchases and /manager API contracts.

Production already owns Purchase Bills, approvals and Stock Entries. This
adapter demonstrates that flow in SQLite; it never contacts Frappe.
"""
import hashlib
import html
import json
from datetime import date, datetime, timedelta, timezone

CATEGORIES = [dict(name='Food / Ingredients', in_stock=1, has_item_list=1),
              dict(name='Not sure', in_stock=0, has_item_list=0),
              dict(name='Packaging', in_stock=0, has_item_list=0),
              dict(name='Staff Meals', in_stock=0, has_item_list=1)]


def initialize(db):
    import purchase_defaults,approval_history
    purchase_defaults.initialize(db);approval_history.initialize(db)
    import approved_purchases
    approved_purchases.initialize(db)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS purchase_bills(
            name TEXT PRIMARY KEY, creation TEXT NOT NULL, requested_by TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            resolved_by TEXT, resolved_at TEXT, comment TEXT, stock_entry TEXT);
        CREATE TRIGGER IF NOT EXISTS immutable_purchase_bill
        BEFORE UPDATE OF name,creation,requested_by,payload ON purchase_bills
        BEGIN SELECT RAISE(ABORT,'Original purchase bills are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_purchase_bill_delete BEFORE DELETE ON purchase_bills
        BEGIN SELECT RAISE(ABORT,'Purchase bill history is immutable'); END;
    ''')
    if 'resolved_payload' not in {r['name'] for r in db.execute('PRAGMA table_info(purchase_bills)')}:
        db.execute('ALTER TABLE purchase_bills ADD COLUMN resolved_payload TEXT')
    db.executescript('''
        CREATE TRIGGER IF NOT EXISTS immutable_resolved_purchase
        BEFORE UPDATE OF resolved_payload ON purchase_bills
        WHEN OLD.status != 'pending'
        BEGIN SELECT RAISE(ABORT,'Approved purchase history is immutable'); END;
    ''')


def pending_quantities(db):
    totals = {}
    for bill in db.execute("SELECT payload FROM purchase_bills WHERE status='pending'"):
        for line in json.loads(bill['payload'])['lines']:
            if line['category'] == 'Food / Ingredients':
                totals[line['item']] = totals.get(line['item'], 0) + line['qty']
    return totals


def bill_dict(bill, db=None):
    payload = json.loads(bill['resolved_payload'] or bill['payload'])
    past = []
    if db is not None:
        import approved_purchases
        past = approved_purchases.history(db, bill['name'])
        if past: payload = past[0]['bill']
    for index, line in enumerate(payload['lines']):
        line['row_name'] = f"{bill['name']}-LINE-{index+1}"
        line['has_item'] = int(bool(line.get('item')))
        category = next((c for c in CATEGORIES if c['name']==line['category']), {})
        line['is_stock'] = category.get('in_stock', 0)
        line['has_item_list'] = category.get('has_item_list', 0)
    original = json.loads(bill['payload'])['lines']
    approval_changes = [dict(name=new['name'], before=old['category'], after=new['category'])
                        for old, new in zip(original, json.loads(bill['resolved_payload'] or bill['payload'])['lines'])
                        if old['category'] != new['category']]
    change = (dict(by=past[0]['by'], when=past[0]['when'], changes=past[0]['changes']) if past else
              dict(by=bill['resolved_by'] or '', when=bill['resolved_at'] or '', changes=approval_changes)
              if approval_changes else None)
    total = sum(line['qty'] * line['price'] for line in payload['lines'])
    return dict(payload, name=bill['name'], status=bill['status'], by=bill['requested_by'],
                time=bill['creation'], date=payload['purchase_date'], total=total, subtotal=total,
                discount=0, categories=sorted({line['category'] for line in payload['lines']}),
                resolved_by=bill['resolved_by'] or '', resolved_when=bill['resolved_at'] or '',
                manager_comment=bill['comment'] or '', stock_entry=bill['stock_entry'],
                category_changed=bool(change), category_change=change,
                payment_status='unpaid', due=bill['status']=='approved')


def approvals(db, actor):
    result = []
    for bill in db.execute('SELECT * FROM purchase_bills ORDER BY creation DESC'):
        data = bill_dict(bill, db)
        lines = [dict(line, name=html.escape(line['name']),
                      category=html.escape(line['category']),
                      qtyStr=f"{line['qty']:g} {line['unit']}",
                      totalStr=f"Rs {line['qty'] * line['price']:,.2f}") for line in data['lines']]
        result.append(dict(name=bill['name'], type='bill', status=bill['status'],
            title=data['vendor'], subhead='Purchase bill', by=bill['requested_by'],
            mine=bill['requested_by']==actor, self_ok=False, time=bill['creation'],
            requested_at=bill['creation'], created_when=bill['creation'],
            resolved_by=bill['resolved_by'] or '',resolved_when=bill['resolved_at'] or '',
            lines=lines, footer_label='Bill total',
            footer_value=f"Rs {data['total']:,.2f}", category_value='',
            line_category_options=[dict(value=c['name'], label=c['name'], **c) for c in CATEGORIES],
            comment=bill['comment'] or '', image=data.get('receipt_image','')))
    return result


def handle(db, method, p, catalog, actor='Local purchaser'):
    from server import number, now
    import purchase_defaults,approval_history
    if method=='purchase_item_categories.catalog':return purchase_defaults.catalog(db,catalog)
    if method=='removal_report.history':return approval_history.history(db,p,actor)
    if method.startswith('purchase_categories.'):
        import approved_purchases
        if method=='purchase_categories.review': return approved_purchases.review(db,p.get('name'),actor)
        if method=='purchase_categories.correct': return approved_purchases.correct(db,p,actor)
        raise ValueError('Unknown category correction action.')
    if method == 'get_purchase_catalog':
        return dict(items=[dict(item=r['item'],name=r['name'],unit=r['stock_uom'],
                               price=r['rate'],group='Ingredients') for r in catalog])
    if method == 'get_expense_categories':
        return dict(categories=[r['name'] for r in CATEGORIES], expense_categories=[r['name'] for r in CATEGORIES if not r['in_stock']], category_details=CATEGORIES)
    if method == 'get_expenses':
        return dict(expenses=[])
    if method == 'get_purchase_vendors':
        names = sorted({json.loads(r[0])['vendor'] for r in db.execute('SELECT payload FROM purchase_bills')})
        return dict(vendors=[dict(name=name,count=1) for name in names])
    if method in ('get_purchase_bills', 'get_due_bills'):
        bills = [bill_dict(r, db) for r in db.execute('SELECT * FROM purchase_bills ORDER BY creation DESC')]
        return dict(bills=[r for r in bills if method!='get_due_bills' or r['due']],
                    pending_amt=sum(r['total'] for r in bills if r['status']=='pending'),
                    approved_today=sum(r['total'] for r in bills if r['status']=='approved' and r['purchase_date']==date.today().isoformat()),
                    week=sum(r['total'] for r in bills if r['status']=='approved'),
                    appr_count=sum(r['status']=='pending' for r in bills), due_count=sum(r['due'] for r in bills))
    if method in ('get_purchase_approvals','get_pending_approvals','get_approvals_digest'):
        rows = approvals(db, actor)
        pending = sum(row['status']=='pending' for row in rows)
        return dict(approvals=rows, pending=pending, sig=hashlib.sha256(json.dumps(rows).encode()).hexdigest())
    if method == 'create_purchase_bill':
        vendor = (p.get('vendor') or '').strip()
        if not 1 <= len(vendor) <= 140: raise ValueError('Enter the vendor name.')
        bought = date.fromisoformat(p.get('purchase_date') or date.today().isoformat())
        if not date.today()-timedelta(days=30) <= bought <= date.today():
            raise ValueError('Use a purchase date within the last 30 days.')
        rows = json.loads(p.get('lines') or '[]')
        if not isinstance(rows,list) or not 1 <= len(rows) <= 100: raise ValueError('Add 1–100 bill items.')
        by_item = {row['item']:row for row in catalog}; lines=[]
        for raw in rows:
            item = by_item.get(raw.get('item'))
            chosen=purchase_defaults.category(db,raw.get('item'),raw.get('name')) if p.get('use_item_categories') else None
            if chosen and raw.get('category') and chosen!=raw['category']:raise ValueError('The saved category changed. Reopen the bill form.')
            category=chosen or raw.get('category')
            if category in ('Not sure','Packaging'):
                label=(raw.get('name') or (item['name'] if item else '')).strip()
                if not label:raise ValueError('Enter an item description.')
                lines.append(dict(item=None,name=label,unit='Bill',qty=1,price=number(raw.get('price'),'Amount')*number(raw.get('qty'),'Quantity'),old_price=0,category=category))
                continue
            if not item: raise ValueError('Choose an existing stock item for this preview.')
            if raw.get('category') not in ('Food / Ingredients','Staff Meals'): raise ValueError('Choose an item-listed category.')
            if raw.get('unit') != item['stock_uom']: raise ValueError('The item unit changed. Reload the bill.')
            quantity = number(raw.get('qty'),'Quantity'); price = number(raw.get('price'),'Unit price')
            if item['stock_uom'].lower()=='nos' and not quantity.is_integer(): raise ValueError('Use whole nos.')
            lines.append(dict(item=item['item'],name=item['name'],unit=item['stock_uom'],
                qty=quantity,price=price,old_price=item['rate'],category=raw['category']))
        name = 'LOCAL-BILL-'+str(db.execute('SELECT count(*) FROM purchase_bills').fetchone()[0]+1).zfill(4)
        payload = dict(vendor=vendor,purchase_date=bought.isoformat(),lines=lines,
                       note=(p.get('note') or '')[:1000],receipt_image='')
        db.execute('INSERT INTO purchase_bills(name,creation,requested_by,payload) VALUES(?,?,?,?)',
                   (name,now(),actor,json.dumps(payload)))
        return dict(ok=True,name=name)
    if method == 'resolve_approval':
        if actor != 'Local manager': raise ValueError('Switch to Manager to review this bill.')
        bill = db.execute('SELECT * FROM purchase_bills WHERE name=?',(p.get('name'),)).fetchone()
        if not bill: raise ValueError('Purchase bill not found.')
        if bill['requested_by']==actor: raise ValueError('Someone else must approve a bill you entered.')
        decision = p.get('decision')
        if decision not in ('approved','rejected'): raise ValueError('Choose Approve or Reject.')
        if bill['status']!='pending':
            if bill['status']==decision: return dict(ok=True,already_processed=True,stock_entry=bill['stock_entry'])
            raise ValueError('This bill has already been resolved.')
        if p.get('discount') or p.get('category'):
            raise ValueError('Discounts and reclassification are outside this inventory preview.')
        receipt=None; movements=[]
        payload = bill_dict(bill, db)
        if decision=='approved':
            changes = p.get('line_categories') or []
            if isinstance(changes, str): changes = json.loads(changes)
            if not isinstance(changes, list): raise ValueError('Line categories must be a list.')
            by_row = {line['row_name']: line for line in payload['lines']}
            categories = {c['name']: c for c in CATEGORIES}
            history = []
            for change in changes:
                if not isinstance(change, dict) or change.get('row_name') not in by_row:
                    raise ValueError('A bill line is missing or belongs to another bill.')
                category = categories.get(change.get('category'))
                if not category: raise ValueError('Pick a valid category for every item.')
                line = by_row[change['row_name']]
                if category['name'] == line['category']: continue
                if category['has_item_list'] and not line.get('item'):
                    raise ValueError('An item-list category needs a linked Item.')
                history.append(dict(row_name=line['row_name'],item=line.get('item'),name=line['name'],before=line['category'],after=category['name']))
                line['category'] = category['name']
                if not category['has_item_list']:
                    line.update(item=None, price=line['qty']*line['price'], qty=1, unit='Bill', old_price=0)
            payload['category_changes'] = history
            purchase_defaults.remember(db,history)
            for line in payload['lines']:
                if line['category']!='Food / Ingredients': continue
                row=db.execute('SELECT * FROM stock WHERE item=?',(line['item'],)).fetchone()
                if not row or row['stock_uom']!=line['unit']: raise ValueError('A stock item or unit changed. Review the bill.')
                after=round(row['balance']+line['qty'],6)
                rate=line['price'] if row['balance']<=0 else (row['balance']*row['rate']+line['qty']*line['price'])/after
                db.execute('UPDATE stock SET balance=?,rate=? WHERE item=?',(after,rate,line['item']))
                movements.append(dict(item=line['item'],name=row['name'],before=row['balance'],after=after,
                                      qty=line['qty'],uom=line['unit'],price=line['price']))
            if movements:
                receipt='LOCAL-RECEIPT-'+bill['name'].removeprefix('LOCAL-BILL-')
                db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(receipt,now(),actor,'purchase_bill',
                    json.dumps(dict(bill=bill['name'],lines=movements))))
        db.execute('UPDATE purchase_bills SET status=?,resolved_by=?,resolved_at=?,comment=?,stock_entry=?,resolved_payload=? WHERE name=?',
                   (decision,actor,now(),(p.get('comment') or '')[:1000],receipt,
                    json.dumps(payload) if decision=='approved' else None,bill['name']))
        return dict(ok=True,stock_entry=receipt)
    if method == 'get_manager_dashboard':
        return dict(revenue=0,sales_count=0,avg_ticket=0,void_count=0,date=date.today().isoformat(),payments=[],hourly=[],top_items=[])
    if method == 'get_activity_flags': return dict(flags=[],count=0)
    raise ValueError('This action is not connected in the local inventory preview.')
