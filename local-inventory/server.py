"""Loopback-only inventory review harness. Never connects to the live service."""
import argparse
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import threading
import purchasing
import existing_purchases
import equipment
import item_names
import native_pages
import sys
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from luuvcrm.inventory_planning import low_stock
from luuvcrm.stock_policy import normalize as normalize_policy, assess as assess_stock, rejection as stock_rejection
ITEM = 'KG-FRIES-CHICKEN'
NAME = 'Crispy Chicken Loaded Fries'
INGREDIENTS = [
    ('KG-STOCK-POTATO', 'Prepared potatoes', 'Kg', 'g', 150),
    ('KG-STOCK-CHICKEN', 'Marinated chicken breast', 'Kg', 'g', 150),
    ('KG-STOCK-SALAD', 'Prepared salad', 'Kg', 'g', 50),
    ('KG-STOCK-WHITE', 'White sauce', 'L', 'ml', 30),
    ('KG-STOCK-SPECIAL', 'Luuv special sauce', 'L', 'ml', 30),
    ('KG-STOCK-HOT', 'Hot sauce', 'L', 'ml', 30),
    ('KG-STOCK-MOZZARELLA', 'Mozzarella cheese', 'Kg', 'g', 20),
    ('KG-STOCK-OIL', 'Cooking oil', 'L', 'ml', 50),
    ('KG-STOCK-FLOUR', 'Flour', 'Kg', 'g', 50),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def number(value, label, zero=False):
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = float('nan')
    if not math.isfinite(n) or n < 0 or (n == 0 and not zero):
        raise ValueError(label + ' must be a valid ' + ('non-negative' if zero else 'positive') + ' number.')
    return n


class Store:
    def __init__(self, path):
        self.changed = threading.Condition()
        self.revision = 0
        self.last_change = {}
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS branch_stock_policies(restaurant TEXT PRIMARY KEY,policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stock(item TEXT PRIMARY KEY,name TEXT,stock_uom TEXT,uom TEXT,balance REAL NOT NULL,rate REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recipes(name TEXT PRIMARY KEY,creation TEXT NOT NULL,docstatus INTEGER NOT NULL,owner TEXT,ingredients TEXT NOT NULL,effective_from TEXT);
                CREATE TABLE IF NOT EXISTS movements(name TEXT PRIMARY KEY,creation TEXT,owner TEXT,kind TEXT,details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(name TEXT PRIMARY KEY,creation TEXT,qty INTEGER NOT NULL,version TEXT NOT NULL,ingredients TEXT NOT NULL,fired INTEGER NOT NULL DEFAULT 0);
                CREATE TRIGGER IF NOT EXISTS immutable_recipe BEFORE UPDATE OF ingredients,creation ON recipes BEGIN SELECT RAISE(ABORT,'Recipe versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_movement BEFORE UPDATE ON movements BEGIN SELECT RAISE(ABORT,'Stock history is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_order BEFORE UPDATE OF ingredients,version,qty ON orders BEGIN SELECT RAISE(ABORT,'Order snapshots are immutable'); END;
            ''')
            if 'minimum_qty' not in [r[1] for r in db.execute('PRAGMA table_info(stock)')]:
                db.execute('ALTER TABLE stock ADD COLUMN minimum_qty REAL NOT NULL DEFAULT 0')
            columns = [r[1] for r in db.execute('PRAGMA table_info(recipes)')]
            if 'item_code' not in columns:
                db.execute("ALTER TABLE recipes ADD COLUMN item_code TEXT NOT NULL DEFAULT 'KG-FRIES-CHICKEN'")
            if 'output_qty' not in columns:
                db.execute('ALTER TABLE recipes ADD COLUMN output_qty REAL NOT NULL DEFAULT 1')
            if 'recipe_note' not in columns:
                db.execute("ALTER TABLE recipes ADD COLUMN recipe_note TEXT NOT NULL DEFAULT ''")
            order_columns=[r[1] for r in db.execute('PRAGMA table_info(orders)')]
            if 'customizations' not in order_columns:db.execute("ALTER TABLE orders ADD COLUMN customizations TEXT NOT NULL DEFAULT '[]'")
            if 'kitchen_note' not in order_columns:db.execute("ALTER TABLE orders ADD COLUMN kitchen_note TEXT NOT NULL DEFAULT ''")
            equipment.initialize(db)
            purchasing.initialize(db)
            existing_purchases.initialize(db)
            db.executescript('''
                CREATE TABLE IF NOT EXISTS recipe_items(item TEXT PRIMARY KEY,kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,name TEXT UNIQUE NOT NULL,creation TEXT NOT NULL,item TEXT NOT NULL,details TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_recipe_output BEFORE UPDATE OF item_code,output_qty ON recipes BEGIN SELECT RAISE(ABORT,'Recipe versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_recipe_note BEFORE UPDATE OF recipe_note ON recipes BEGIN SELECT RAISE(ABORT,'Recipe versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_batch BEFORE UPDATE ON batches BEGIN SELECT RAISE(ABORT,'Completed batches are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_batch_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT,'Completed batches are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_order_changes BEFORE UPDATE OF customizations,kitchen_note ON orders BEGIN SELECT RAISE(ABORT,'Order snapshots are immutable'); END;
                CREATE TABLE IF NOT EXISTS order_revisions(original TEXT PRIMARY KEY,replacement TEXT UNIQUE NOT NULL,creation TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_order_revision BEFORE UPDATE ON order_revisions BEGIN SELECT RAISE(ABORT,'Order revision history is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_order_revision_delete BEFORE DELETE ON order_revisions BEGIN SELECT RAISE(ABORT,'Order revision history is immutable'); END;
            ''')
            db.execute('INSERT OR IGNORE INTO recipe_items VALUES(?,?)',(ITEM,'menu'))
            if not db.execute('SELECT count(*) FROM stock').fetchone()[0]:
                db.executemany('INSERT INTO stock(item,name,stock_uom,uom,balance,rate) VALUES(?,?,?,?,0,0)', [(a,b,c,d) for a,b,c,d,e in INGREDIENTS])
                rows = [dict(item=a,name=b,qty=e,uom=d) for a,b,c,d,e in INGREDIENTS]
                db.execute('INSERT INTO recipes(name,creation,docstatus,owner,ingredients,effective_from) VALUES(?,?,0,?,?,NULL)', ('LOCAL-RECIPE-0001',now(),'Local manager',json.dumps(rows)))
            # Link the existing salad stock item; never replace its quantity or
            # invent a preparation recipe when the actual vegetables are unknown.
            if db.execute("SELECT 1 FROM stock WHERE item='KG-STOCK-SALAD'").fetchone():
                db.execute("INSERT OR IGNORE INTO recipe_items VALUES('KG-STOCK-SALAD','prepared')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def catalog(self,db,include_equipment=False):
        main=equipment.settings(db)["items"]
        prepared={r[0] for r in db.execute("SELECT item FROM recipe_items WHERE kind='prepared'")}
        pending=purchasing.pending_quantities(db)
        approvals=existing_purchases.pending_quantities(db)
        return [dict(r, factor=1000 if r['stock_uom'] in ('Kg','L') else 1,prepared=r['item'] in prepared,on_order=pending.get(r['item'],0),pending_qty=approvals.get(r['item'],0)) for r in db.execute('SELECT * FROM stock ORDER BY name') if include_equipment or r['item'] not in main]

    def stock_policy(self,db,restaurant='Kurunegala'):
        row=db.execute('SELECT policy FROM branch_stock_policies WHERE restaurant=?',(restaurant,)).fetchone()
        return normalize_policy(json.loads(row['policy']) if row else None)

    def recipe(self,db,version=None,item=ITEM):
        target=db.execute('SELECT * FROM recipe_items WHERE item=?',(item,)).fetchone()
        if not target:raise ValueError('Choose a recipe from this branch.')
        stock=db.execute('SELECT * FROM stock WHERE item=?',(item,)).fetchone()
        versions = [dict(r) for r in db.execute('SELECT name,creation,docstatus,owner,effective_from AS luuv_recipe_effective_from FROM recipes WHERE item_code=? ORDER BY creation DESC,rowid DESC',(item,))]
        latest=versions[0]['name'] if versions else ''
        selected = version or latest
        r = db.execute('SELECT * FROM recipes WHERE name=? AND item_code=?',(selected,item)).fetchone()
        if selected and not r:
            raise ValueError('Recipe version not found.')
        batches=[dict(name=b['name'],creation=b['creation'],details=json.loads(b['details'])) for b in db.execute('SELECT * FROM batches WHERE item=? ORDER BY creation DESC,rowid DESC LIMIT 25',(item,))]
        return dict(item=item,name=NAME if item==ITEM else stock['name'],kind=target['kind'],latest=latest,version=r['name'] if r else '',
            status='Active' if r and r['docstatus']==1 else 'Draft',ingredients=json.loads(r['ingredients']) if r else [],
            output_qty=r['output_qty'] if r else (1000 if stock and stock['stock_uom'] in ('Kg','L') else 1),
            recipe_note=r['recipe_note'] if r else '',
            output_uom=stock['uom'] if stock else 'portion',stock_uom=stock['stock_uom'] if stock else '',
            balance=stock['balance'] if stock else 0,batches=batches,
            versions=versions,catalog=self.catalog(db),restaurant='Kurunegala')

    def check_cycle(self,db,item,ingredients):
        graph={}
        for target in db.execute("SELECT item FROM recipe_items WHERE kind='prepared'"):
            r=db.execute('SELECT ingredients FROM recipes WHERE item_code=? ORDER BY creation DESC,rowid DESC LIMIT 1',(target['item'],)).fetchone()
            graph[target['item']]=[row['item'] for row in json.loads(r['ingredients'])] if r else []
        graph[item]=[row['item'] for row in ingredients]
        def visit(node,path):
            if node in path:raise ValueError('A prepared recipe cannot contain itself, directly or through another recipe.')
            for child in graph.get(node,[]):visit(child,path|{node})
        visit(item,set())

    def complete_batch(self,db,p):
        request_id=(p.get('request_id') or '').strip()
        if not 8<=len(request_id)<=100:raise ValueError('Reopen the batch form before completing it.')
        item=p.get('item_code');version=p.get('version')
        planned=number(p.get('planned_qty'),'Recipe quantity')
        actual=number(p.get('actual_qty'),'Finished quantity')
        comment=(p.get('comment') or '').strip()
        if len(comment)>1000:raise ValueError('Keep the batch note under 1000 characters.')
        if planned!=actual and len(comment)<3:raise ValueError('Add a note for the difference in finished quantity.')
        payload_hash=hashlib.sha256(json.dumps([item,version,planned,actual,comment]).encode()).hexdigest()
        existing=db.execute('SELECT * FROM batches WHERE request_id=?',(request_id,)).fetchone()
        if existing:
            if existing['payload_hash']!=payload_hash:raise ValueError('This batch was already completed with different values. Reopen the form.')
            return dict(json.loads(existing['details']),already_processed=True)
        target=db.execute("SELECT 1 FROM recipe_items WHERE item=? AND kind='prepared'",(item,)).fetchone()
        if not target:raise ValueError('Choose a prepared item.')
        recipe=db.execute('SELECT * FROM recipes WHERE item_code=? AND docstatus=1 ORDER BY effective_from DESC,rowid DESC LIMIT 1',(item,)).fetchone()
        if not recipe or recipe['name']!=version:raise ValueError('The active preparation recipe changed. Reopen the batch form.')
        catalog={r['item']:r for r in self.catalog(db)};output=catalog[item]
        if output['stock_uom']=='nos' and (planned!=int(planned) or actual!=int(actual)):
            raise ValueError('Enter a whole number of nos.')
        output_qty=round(actual/output['factor'],6)
        if output_qty<=0:raise ValueError('Finished quantity is too small for the stock unit.')
        deductions=[];cost=0
        for row in json.loads(recipe['ingredients']):
            ingredient=catalog[row['item']]
            raw_used=row['qty']*planned/recipe['output_qty']/ingredient['factor']
            if ingredient['stock_uom']=='nos' and abs(raw_used-round(raw_used))>1e-9:
                raise ValueError('This batch needs a fraction of '+ingredient['name']+'. Choose a quantity using whole nos.')
            used=round(raw_used,6)
            if used<=0:raise ValueError('Batch quantity is too small for '+ingredient['name']+'.')
            if ingredient['balance']+1e-9<used:raise ValueError('Insufficient stock: '+ingredient['name'])
            cost+=used*ingredient['rate']
            deductions.append(dict(item=row['item'],name=row['name'],qty=used,uom=ingredient['stock_uom'],before=ingredient['balance'],after=round(ingredient['balance']-used,6)))
        after=round(output['balance']+output_qty,6)
        rate=cost/output_qty if output['balance']<=0 else (output['balance']*output['rate']+cost)/after
        for row in deductions:db.execute('UPDATE stock SET balance=? WHERE item=?',(row['after'],row['item']))
        db.execute('UPDATE stock SET balance=?,rate=? WHERE item=?',(after,rate,item))
        key='LOCAL-BATCH-'+str(db.execute('SELECT count(*) FROM batches').fetchone()[0]+1).zfill(4)
        stamp=now()
        details=dict(name=key,item=item,item_name=output['name'],recipe=version,recipe_note=recipe['recipe_note'],recipe_ingredients=json.loads(recipe['ingredients']),recipe_output_qty=recipe['output_qty'],planned_qty=planned,actual_qty=actual,output_uom=output['uom'],received_qty=output_qty,stock_uom=output['stock_uom'],before=output['balance'],after=after,ingredients=deductions,comment=comment,cost=round(cost,6),owner='Local manager')
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',(request_id,payload_hash,key,stamp,item,json.dumps(details)))
        db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(key,stamp,'Local manager','preparation',json.dumps(details)))
        return details

    def audit(self,db,p):
        row = db.execute('SELECT * FROM stock WHERE item=?',(p.get('item_code'),)).fetchone()
        if not row:
            raise ValueError('Choose a valid ingredient.')
        comment = (p.get('comment') or '').strip()
        if not 3 <= len(comment) <= 1000:
            raise ValueError('Add a reason for the stock correction (3–1000 characters).')
        counted = number(p.get('counted_qty'),'Counted quantity',True)
        if row['stock_uom'].lower()=='nos' and counted!=int(counted):
            raise ValueError('Enter a whole number of nos.')
        try:expected=float(p.get('expected_qty'))
        except (TypeError,ValueError):expected=float('nan')
        if not math.isfinite(expected):raise ValueError('Expected quantity must be a finite number.')
        if abs(row['balance']-expected)>1e-6:
            raise ValueError('Stock changed while you counted. Refresh and check the latest quantity.')
        if abs(row['balance']-counted)<1e-6:
            raise ValueError('The count matches. No correction is needed.')
        rate = row['rate']
        if counted>0 and rate<=0:
            rate=number(p.get('valuation_rate'),'Cost per stock unit')
        details=dict(item=row['item'],name=row['name'],uom=row['stock_uom'],before=row['balance'],counted=counted,difference=round(counted-row['balance'],6),comment=comment)
        key='LOCAL-AUDIT-'+str(db.execute('SELECT count(*) FROM movements').fetchone()[0]+1).zfill(4)
        db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(key,now(),'Local manager','audit',json.dumps(details)))
        db.execute('UPDATE stock SET balance=?,rate=? WHERE item=?',(counted,rate,row['item']))
        return dict(name=key,details=details)

    def dispatch(self,method,p):
        # _dispatch exits its transaction before notifying browser streams.
        # Failed transactions and duplicate kitchen sends publish nothing.
        result = self._dispatch(method,p)
        if method in ('rename_inventory_item','move_inventory_item','fire_order','correct_stock','set_minimum','create_ingredient','demo_stock','save_recipe','activate_recipe','create_prepared_recipe','complete_batch','save_purchase','receive_purchase','cancel_purchase','native:create_purchase_bill','native:resolve_approval','native:purchase_categories.correct','save_stock_policy') and not result.get('already_processed'):
            with self.changed:
                self.revision += 1
                self.last_change = dict(revision=self.revision,kind=method)
                self.changed.notify_all()
        return result

    def _dispatch(self,method,p):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if p.get('restaurant') not in (None,'','Kurunegala'):
                raise ValueError('Switch to that branch before managing its stock.')
            item_code=p.get('item_code') or ITEM
            if method=='rename_inventory_item':return item_names.rename(db,p)
            if method=='move_inventory_item':return equipment.move(db,p)
            if method=='save_stock_policy':
                if p.get('_actor') not in (None,'Local manager'):raise ValueError('Only Manager can change branch stock rules.')
                before=self.stock_policy(db)
                raw=p.get('policy');after=normalize_policy(json.loads(raw) if isinstance(raw,str) else raw)
                if after['revision']!=before['revision']:raise ValueError('Stock rules changed on another screen. Reload before saving.')
                if set(after['items'])-{r['item'] for r in self.catalog(db)}:raise ValueError('Choose enabled stock ingredients for the rules.')
                after['revision']+=1
                db.execute('INSERT INTO branch_stock_policies VALUES(?,?) ON CONFLICT(restaurant) DO UPDATE SET policy=excluded.policy',('Kurunegala',json.dumps(after)))
                key='POLICY-KG-'+str(after['revision']).zfill(4)
                db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',(key,now(),'Local manager','stock_policy',json.dumps(dict(restaurant='Kurunegala',before=before,after=after))))
                return after
            if method.startswith('native:'):
                return existing_purchases.handle(db,method[7:],p,self.catalog(db,include_equipment=True),p.get('_actor','Local purchaser'))
            if method=='restock':
                return dict(restaurant='Kurunegala',warehouse='Local review stock',
                    stock_categories=['Food / Ingredients'],
                    items=low_stock(self.catalog(db),existing_purchases.pending_quantities(db)))
            if method in ('purchases','save_purchase','receive_purchase','cancel_purchase'):
                return purchasing.handle(db,method,p,self.catalog(db))
            if method=='dashboard':
                menu=[]
                for target in db.execute('SELECT * FROM recipe_items ORDER BY kind,item'):
                    latest=db.execute('SELECT docstatus FROM recipes WHERE item_code=? ORDER BY creation DESC,rowid DESC LIMIT 1',(target['item'],)).fetchone()
                    name=NAME if target['item']==ITEM else db.execute('SELECT name FROM stock WHERE item=?',(target['item'],)).fetchone()['name']
                    menu.append(dict(name=target['item'],item_name=name,kind=target['kind'],recipe_status=('Active' if latest['docstatus'] else 'Draft') if latest else 'Recipe needed'))
                audits=[dict(r) for r in db.execute("SELECT name,creation,owner,details FROM movements WHERE kind='audit' ORDER BY creation DESC,rowid DESC LIMIT 25")]
                for a in audits:a['details']=json.loads(a['details'])
                shortage_sales=[]
                for record in db.execute("SELECT creation,details FROM movements WHERE kind='sale' ORDER BY creation DESC,rowid DESC LIMIT 25"):
                    details=json.loads(record['details'])
                    if details.get('warnings'):shortage_sales.append(dict(details,creation=record['creation']))
                return dict(**equipment.view(db,self.catalog(db,include_equipment=True)),restaurant='Kurunegala',warehouse='Local review stock',as_of=now(),menu=menu,stock=self.catalog(db),audits=audits,uoms=['Kg','L','nos','g','ml'],preparation_recipes=True,purchasing=True,purchasing_flow='bill_approval',stock_policy=self.stock_policy(db),shortage_sales=shortage_sales,realtime=dict(transport='sse',url='/events/stock'))
            if method=='create_prepared_recipe':
                if not db.execute('SELECT 1 FROM stock WHERE item=?',(item_code,)).fetchone():raise ValueError('Choose the finished stock item first.')
                if db.execute('SELECT 1 FROM recipe_items WHERE item=?',(item_code,)).fetchone():raise ValueError('That item already has a recipe in the list.')
                db.execute("INSERT INTO recipe_items VALUES(?,'prepared')",(item_code,))
                return self.recipe(db,item=item_code)
            if method=='complete_batch':return self.complete_batch(db,p)
            if method=='create_ingredient':
                name=(p.get('item_name') or '').strip()
                if not 2<=len(name)<=80:raise ValueError('Enter an ingredient name (2–80 characters).')
                unit=p.get('uom')
                if unit not in ('Kg','L','nos','g','ml'):raise ValueError('Choose a defined unit.')
                if db.execute('SELECT 1 FROM stock WHERE lower(name)=lower(?)',(name,)).fetchone():
                    raise ValueError('That ingredient already exists. Choose it from the stock list.')
                minimum=number(p.get('minimum_qty',0),'Minimum stock level',True)
                if unit=='nos' and minimum!=int(minimum):raise ValueError('Enter a whole number of nos.')
                key='LOCAL-ING-'+str(db.execute('SELECT count(*) FROM stock').fetchone()[0]+1).zfill(4)
                db.execute('INSERT INTO stock(item,name,stock_uom,uom,balance,rate,minimum_qty) VALUES(?,?,?,?,0,0,?)',
                    (key,name,unit,{'Kg':'g','L':'ml'}.get(unit,unit),minimum))
                return dict(item=key,name=name,stock_uom=unit,minimum_qty=minimum)
            if method=='recipe':
                return self.recipe(db,p.get('version'),item_code)
            if method=='save_recipe':
                current=self.recipe(db,item=item_code)
                if p.get('expected_version','')!=current['latest']:
                    raise ValueError('This recipe changed on another screen. Reload before saving.')
                rows=json.loads(p['ingredients']) if isinstance(p.get('ingredients'),str) else p.get('ingredients')
                if not isinstance(rows,list) or not 1<=len(rows)<=100:
                    raise ValueError('Add between 1 and 100 ingredients.')
                catalog={r['item']:r for r in self.catalog(db)}
                merged={}
                for row in rows:
                    if not isinstance(row,dict) or row.get('item') not in catalog:
                        raise ValueError('Choose a valid stock ingredient.')
                    item=row['item'];quantity=number(row.get('qty'),'Ingredient quantity')
                    if catalog[item]['stock_uom'].lower()=='nos' and quantity!=int(quantity):
                        raise ValueError('Enter a whole number of nos for '+catalog[item]['name']+'.')
                    merged[item]=merged.get(item,0)+quantity
                normalized=[dict(item=k,name=catalog[k]['name'],qty=v,uom=catalog[k]['uom']) for k,v in merged.items()]
                output_qty=number(p.get('output_qty',current['output_qty']),'Recipe output quantity') if current['kind']=='prepared' else 1
                if current['output_uom']=='nos' and output_qty!=int(output_qty):raise ValueError('Enter a whole number of nos.')
                recipe_note=(p.get('recipe_note',current['recipe_note']) or '').strip()
                if len(recipe_note)>1000:raise ValueError('Keep the recipe note under 1000 characters.')
                self.check_cycle(db,item_code,normalized)
                key='LOCAL-RECIPE-'+str(db.execute('SELECT count(*) FROM recipes').fetchone()[0]+1).zfill(4)
                db.execute('INSERT INTO recipes(name,creation,docstatus,owner,ingredients,effective_from,item_code,output_qty,recipe_note) VALUES(?,?,0,?,?,NULL,?,?,?)',(key,now(),'Local manager',json.dumps(normalized),item_code,output_qty,recipe_note))
                return self.recipe(db,item=item_code)
            if method=='activate_recipe':
                current=self.recipe(db,item=item_code)
                if not current['version'] or p.get('version')!=current['latest'] or current['status']!='Draft':
                    raise ValueError('Reload the latest draft before activating.')
                self.check_cycle(db,item_code,current['ingredients'])
                catalog={r['item']:r for r in self.catalog(db)}
                checked=assess_stock(self.stock_policy(db),[dict(item=r['item'],name=r['name'],before=catalog[r['item']]['balance'],qty=r['qty']/catalog[r['item']]['factor'],uom=catalog[r['item']]['stock_uom']) for r in current['ingredients']])
                missing=[r['name'] for r in checked['blocked']]
                if missing and current['kind']=='menu':
                    raise ValueError('Record opening stock or a receipt before activating: '+', '.join(missing))
                db.execute('UPDATE recipes SET docstatus=1,effective_from=? WHERE name=?',(now(),p['version']))
                return self.recipe(db,item=item_code)
            if method=='correct_stock':return self.audit(db,p)
            if method=='set_minimum':
                row=db.execute('SELECT * FROM stock WHERE item=?',(p.get('item_code'),)).fetchone()
                if not row:raise ValueError('Choose a valid stock item.')
                minimum=number(p.get('minimum_qty'),'Minimum stock level',True)
                if row['stock_uom'].lower()=='nos' and minimum!=int(minimum):
                    raise ValueError('Enter a whole number of nos.')
                db.execute('UPDATE stock SET minimum_qty=? WHERE item=?',(minimum,row['item']))
                return dict(item=row['item'],minimum_qty=minimum,uom=row['stock_uom'])
            if method=='demo_stock':
                for row in self.catalog(db):
                    if row['balance']<10:
                        self.audit(db,dict(item_code=row['item'],expected_qty=row['balance'],counted_qty=10,valuation_rate=1000,comment='Local testing stock only — not a real branch count.'))
                return dict(ok=True)
            if method=='order_recipe':
                if p.get('order'):
                    order=self.waiting_order(db,p['order'])
                    return dict(self.recipe(db,order['version'],ITEM),active=True,revising=order['name'],qty=order['qty'],customizations=json.loads(order['customizations']),kitchen_note=order['kitchen_note'])
                active=db.execute('SELECT name FROM recipes WHERE item_code=? AND docstatus=1 ORDER BY effective_from DESC,rowid DESC LIMIT 1',(ITEM,)).fetchone()
                if not active:return dict(active=False,catalog=self.catalog(db),ingredients=[])
                return dict(self.recipe(db,active['name'],ITEM),active=True,stock_policy=self.stock_policy(db))
            if method=='create_order':
                qty=number(p.get('qty'),'Portions')
                if qty!=int(qty) or qty>1000:raise ValueError('Enter a whole number from 1 to 1000.')
                original=self.waiting_order(db,p['replaces_order']) if p.get('replaces_order') else None
                recipe=db.execute('SELECT * FROM recipes WHERE name=?',(original['version'],)).fetchone() if original else db.execute('SELECT * FROM recipes WHERE item_code=? AND docstatus=1 ORDER BY effective_from DESC,rowid DESC LIMIT 1',(ITEM,)).fetchone()
                if not recipe:raise ValueError('Activate a recipe in Manager before creating a local test order.')
                if p.get('expected_version') and p['expected_version']!=recipe['name']:raise ValueError('The menu recipe changed. Reload the order options before placing it.')
                ingredients,customizations=self.customize_order(db,recipe,p)
                note=(p.get('kitchen_note') or '').strip()
                if len(note)>500:raise ValueError('Keep the kitchen note under 500 characters.')
                key='LOCAL-ORDER-'+str(db.execute('SELECT count(*) FROM orders').fetchone()[0]+1).zfill(4)
                db.execute('INSERT INTO orders(name,creation,qty,version,ingredients,fired,customizations,kitchen_note) VALUES(?,?,?,?,?,0,?,?)',(key,now(),int(qty),recipe['name'],json.dumps(ingredients),json.dumps(customizations),note))
                if original:db.execute('INSERT INTO order_revisions VALUES(?,?,?)',(original['name'],key,now()))
                return dict(name=key,version=recipe['name'],qty=int(qty),customizations=customizations,kitchen_note=note)
            if method=='fire_order':
                order=db.execute('SELECT * FROM orders WHERE name=?',(p.get('order'),)).fetchone()
                if not order:raise ValueError('Order not found.')
                superseded=db.execute('SELECT replacement FROM order_revisions WHERE original=?',(order['name'],)).fetchone()
                if superseded:raise ValueError('This order was replaced by '+superseded['replacement']+'. Send the revised order instead.')
                if order['fired']:
                    saved=db.execute('SELECT details FROM movements WHERE name=?',('ISSUE-'+order['name'],)).fetchone()
                    return dict(json.loads(saved['details']) if saved else {},name=order['name'],already_processed=True)
                catalog={r['item']:r for r in self.catalog(db)}
                deductions=[]
                for row in json.loads(order['ingredients']):
                    ingredient=catalog[row['item']];used=round(row['qty']*order['qty']/ingredient['factor'],6)
                    deductions.append(dict(item=row['item'],name=row['name'],qty=used,uom=ingredient['stock_uom'],before=ingredient['balance'],after=round(ingredient['balance']-used,6)))
                policy=self.stock_policy(db);checked=assess_stock(policy,deductions)
                if checked['blocked']:raise ValueError(stock_rejection(checked['blocked']))
                for row in deductions:db.execute('UPDATE stock SET balance=? WHERE item=?',(row['after'],row['item']))
                details=dict(order=order['name'],recipe=order['version'],portions=order['qty'],ingredients=deductions,customizations=json.loads(order['customizations']),kitchen_note=order['kitchen_note'],policy=policy,warnings=checked['warnings'])
                db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',('ISSUE-'+order['name'],now(),'Local POS','sale',json.dumps(details)))
                db.execute('UPDATE orders SET fired=1 WHERE name=?',(order['name'],))
                return details
            if method=='orders':
                rows=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY creation DESC,rowid DESC')]
                revisions={r['original']:r['replacement'] for r in db.execute('SELECT * FROM order_revisions')}
                originals={v:k for k,v in revisions.items()}
                for row in rows:
                    row['ingredients']=json.loads(row['ingredients']);row['customizations']=json.loads(row['customizations'])
                    row['superseded_by']=revisions.get(row['name'],'');row['revises']=originals.get(row['name'],'')
                    record=db.execute('SELECT details FROM movements WHERE name=?',('ISSUE-'+row['name'],)).fetchone()
                    row['stock_warnings']=json.loads(record['details']).get('warnings',[]) if record else []
                return rows
            raise ValueError('Unknown local operation.')

    def waiting_order(self,db,name):
        order=db.execute('SELECT * FROM orders WHERE name=?',(name,)).fetchone()
        if not order:raise ValueError('Order not found.')
        if order['fired']:raise ValueError('Stock has already been deducted for this order. It cannot be revised.')
        if db.execute('SELECT 1 FROM order_revisions WHERE original=?',(name,)).fetchone():raise ValueError('This order already has a replacement. Open the latest order.')
        return order

    def customize_order(self,db,recipe,p):
        original=json.loads(recipe['ingredients']);by_item={i['item']:i for i in original}
        rows=p.get('customizations') or []
        if isinstance(rows,str):rows=json.loads(rows)
        if not isinstance(rows,list) or len(rows)>len(original):raise ValueError('Choose valid changes for this order.')
        catalog={r['item']:r for r in self.catalog(db)};changes={};snapshots=[]
        for row in rows:
            if not isinstance(row,dict) or row.get('item') not in by_item:raise ValueError('Choose an ingredient from this recipe to change.')
            item=row['item'];source=by_item[item]
            if item in changes:raise ValueError('Choose one change per ingredient.')
            action=row.get('action')
            if action not in ('omit','substitute'):raise ValueError('Choose Without or Replace.')
            snapshot=dict(item=item,name=source['name'],qty=source['qty'],uom=source['uom'],action=action)
            replacement=None
            if action=='substitute':
                target=catalog.get(row.get('replacement'))
                if not target or target['item']==item:raise ValueError('Choose a different replacement ingredient.')
                amount=number(row.get('qty'),'Replacement quantity per portion')
                if target['stock_uom']=='nos' and amount!=int(amount):raise ValueError('Enter a whole number of nos.')
                if round(amount/target['factor'],6)<=0:raise ValueError('Replacement quantity is too small.')
                replacement=dict(item=target['item'],name=target['name'],qty=amount,uom=target['uom'])
                snapshot['replacement']=replacement
            changes[item]=replacement;snapshots.append(snapshot)
        merged={}
        for ingredient in original:
            effective=changes[ingredient['item']] if ingredient['item'] in changes else ingredient
            if effective is None:continue
            if effective['item'] in merged:merged[effective['item']]['qty']+=effective['qty']
            else:merged[effective['item']]=dict(effective)
        if not merged:raise ValueError('An order must still contain at least one ingredient.')
        return list(merged.values()),snapshots


SHELL='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Luuv Fryxo · Manager Inventory</title><style>body{margin:0;background:#faf7f2;color:#24201c;font-family:system-ui,sans-serif}header{background:#fff;border-bottom:1px solid #e8e1d9;padding:20px 5%;display:flex;justify-content:space-between;align-items:center;gap:15px}header strong{font-size:20px;letter-spacing:-.6px}header span{color:#e8510a}header a{color:#d65010;text-decoration:none;font-weight:650;font-size:13px}.local{background:#2d2925;color:#fff;padding:9px 5%;font-size:12px;display:flex;justify-content:space-between;gap:12px}.local b{color:#ffc68f}main{max-width:1180px;margin:32px auto;padding:0 22px}footer{text-align:center;color:#978b80;font-size:12px;padding:30px}.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}@media(max-width:700px){.local{display:block;line-height:1.8}.pos-grid{grid-template-columns:1fr}header{padding:18px 22px}main{padding:0 15px}}</style></head><body><div class="local"><div><b>LOCAL REVIEW</b> · Separate test data · Live branch stock is unchanged</div><div>Kurunegala / Manager inventory</div></div><header><strong><span>luuv</span> fryxo <small style="font-size:12px;color:#8c7f73">MANAGER</small></strong><div style="display:flex;gap:18px;flex-wrap:wrap"><a href="/purchases">Purchasing ↗</a><a href="/pos">Open local POS test ↗</a></div></header><main><div id="tabInventory"></div></main><footer>Recipe versions · Branch stock · Count corrections with a reason</footer>__COMPONENT__<script>LFInventory.load();</script></body></html>'''


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,body,mime='application/json',actor=None):
            raw=body.encode('utf-8');self.send_response(status);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
            if actor:self.send_header('Set-Cookie','preview_role='+actor+'; Path=/; SameSite=Strict; HttpOnly')
            self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            path=urlparse(self.path).path
            if path=='/events/stock':
                self.stream_stock();return
            if path in native_pages.PAGES:
                page=native_pages.PAGES[path]
                self.send(200,native_pages.render(page),'text/html',actor='manager' if page in ('manager','manager_v2') else 'purchaser');return
            if path!='/pos':
                self.send(404,'Not found','text/plain');return
            component=(ROOT/'luuvcrm/templates/includes/manager_inventory.html').read_text(encoding='utf-8')
            page=(Path(__file__).parent/(path.strip('/')+'.html')).read_text(encoding='utf-8') if path in ('/pos','/purchases') else SHELL
            self.send(200,page.replace('__COMPONENT__',component),'text/html')
        def stream_stock(self):
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):
                self.send(403,json.dumps(dict(error='Local requests only.')));return
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache, no-transform')
            self.send_header('X-Accel-Buffering','no')
            self.end_headers()
            self.close_connection=True
            def event(kind,payload):
                self.wfile.write(('event: '+kind+'\ndata: '+json.dumps(payload)+'\n\n').encode())
                self.wfile.flush()
            try:
                with store.changed:revision=store.revision
                # Every connection, including a reconnect after an outage,
                # requests a fresh committed snapshot. No replay gap survives.
                self.wfile.write(b'retry: 1000\n\n')
                event('ready',dict(revision=revision))
                while True:
                    with store.changed:
                        updated=store.changed.wait_for(lambda:store.revision!=revision,timeout=15)
                        revision=store.revision
                        change=dict(store.last_change)
                    if updated:event('stock',change)
                    else:
                        self.wfile.write(b': heartbeat\n\n');self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,OSError):
                pass
        def do_POST(self):
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):
                self.send(403,json.dumps(dict(error='Only same-origin local requests are allowed.')));return
            prefix='/api/method/luuvcrm.api.'
            if not self.path.startswith(prefix):self.send(404,'{}');return
            length=int(self.headers.get('Content-Length','0'))
            if length>100000:self.send(413,'{}');return
            params={k:v[-1] for k,v in parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True).items()}
            method=self.path[len(prefix):]
            if method.startswith('manager_inventory.'):method=method[len('manager_inventory.'):]
            elif method=='inventory_purchasing.restock':method='restock'
            else:method='native:'+method
            cookies=SimpleCookie(self.headers.get('Cookie',''))
            role=cookies.get('preview_role')
            preview_role=role.value if role else 'purchaser'
            # Two local app tabs can be open together. The referring preview
            # screen selects its role; one tab's cookie must not change another.
            # This is a loopback review convenience, never production auth.
            referring=urlparse(self.headers.get('Referer',''))
            if referring.netloc==self.headers.get('Host'):
                if referring.path in ('/manager','/manager2.0','/'):preview_role='manager'
                elif referring.path in ('/purchases','/purchase2.0'):preview_role='purchaser'
            params['_actor']='Local manager' if preview_role=='manager' else 'Local purchaser'
            try:self.send(200,json.dumps(dict(message=store.dispatch(method,params))))
            except (ValueError,KeyError,json.JSONDecodeError) as e:self.send(400,json.dumps(dict(error=str(e),_server_messages=json.dumps([json.dumps(dict(message=str(e)))]))))
            except Exception as e:self.send(500,json.dumps(dict(error='Local preview error: '+str(e))))
    return Handler


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=4192);parser.add_argument('--db',default=str(Path(__file__).parent/'data/preview.sqlite3'));args=parser.parse_args()
    app=ThreadingHTTPServer(('127.0.0.1',args.port),handler(Store(args.db)))
    print('Local manager: http://127.0.0.1:'+str(args.port)+'/manager2.0?tab=inventory',flush=True)
    app.serve_forever()
