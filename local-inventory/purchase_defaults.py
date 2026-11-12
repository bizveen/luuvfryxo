"""Local adapter for durable branch purchase defaults."""
import json

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS purchase_item_defaults(key TEXT PRIMARY KEY,details TEXT NOT NULL)')

def key(name):return ' '.join((name or '').split()).casefold()

def remember(db,changes,lines=()):
    rows={r['row_name']:r for r in lines}
    for change in changes:
        row=rows.get(change.get('row_name'),change)
        category=change.get('after') or change.get('to')
        value=dict(category=category,item=row.get('item'),name=row.get('name'))
        for ident in filter(None,('item:'+row['item'] if row.get('item') else '', 'name:'+key(row.get('name')) if row.get('name') else '')):
            db.execute('INSERT INTO purchase_item_defaults VALUES(?,?) ON CONFLICT(key) DO UPDATE SET details=excluded.details',(ident,json.dumps(value)))

def category(db,item=None,name=None):
    for ident in ('item:'+str(item or ''),'name:'+key(name)):
        row=db.execute('SELECT details FROM purchase_item_defaults WHERE key=?',(ident,)).fetchone()
        if row:return json.loads(row[0])['category']
    return None

def catalog(db,items):
    rows=[dict(item=r['item'],name=r['name'],unit=r['stock_uom'],price=r['rate'],group='Ingredients',
               default_category=category(db,r['item'],r['name']) or '') for r in items]
    known={key(r['name']) for r in rows}
    for record in db.execute("SELECT details FROM purchase_item_defaults WHERE key LIKE 'name:%'"):
        value=json.loads(record[0])
        if key(value['name']) not in known:
            rows.append(dict(item='saved:'+key(value['name']),name=value['name'],unit='Bill',price=0,group='Saved purchases',default_category=value['category'],saved_description=True))
    return dict(items=rows)
