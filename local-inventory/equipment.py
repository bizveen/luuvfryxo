"""Local equipment classification; stock balances and receipts are retained."""
import json

GROUPS = ['Kitchen Equipment','Electrical Equipment','Furniture & Fixtures','Office Equipment','Other Equipment']


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS inventory_sections(branch TEXT PRIMARY KEY, settings TEXT NOT NULL)')


def settings(db):
    row=db.execute("SELECT settings FROM inventory_sections WHERE branch='Kurunegala'").fetchone()
    return json.loads(row[0]) if row else dict(revision=0,items={})


def view(db, catalog):
    config=settings(db)
    return dict(main_inventory=[dict(r,source='stock',equipment_category=config['items'][r['item']]['category'],
        section_note=config['items'][r['item']]['note']) for r in catalog if r['item'] in config['items']],
        section_revision=config['revision'],equipment_categories=GROUPS)


def move(db,p):
    from server import now
    if p.get('_actor') not in (None,'Local manager'):raise ValueError('Only a manager can move inventory items.')
    section=p.get('section');category=p.get('category');comment=(p.get('comment') or '').strip()
    if section not in ('main','running'):raise ValueError('Choose Main inventory or Running stock.')
    if not 3<=len(comment)<=500:raise ValueError('Add a short reason for this move.')
    if section=='main' and category not in GROUPS:raise ValueError('Choose an equipment category.')
    data=settings(db)
    if str(p.get('expected_revision'))!=str(data['revision']):raise ValueError('Inventory sections changed. Refresh and try again.')
    row=db.execute('SELECT * FROM stock WHERE item=?',(p.get('item_code'),)).fetchone()
    if not row:raise ValueError('Choose an item in this branch inventory.')
    item=row['item'];old=data['items'].get(item)
    if section=='main':
        if db.execute('SELECT 1 FROM recipe_items WHERE item=?',(item,)).fetchone():raise ValueError('This item is used by a menu or recipe.')
        for recipe in db.execute('SELECT ingredients FROM recipes WHERE docstatus<2'):
            if any(r['item']==item for r in json.loads(recipe[0])):raise ValueError('This item is used by a menu or recipe.')
        if old and old['category']==category:return dict(ok=True,changed=False)
        data['items'][item]=dict(category=category,note=comment)
    else:
        if not old:return dict(ok=True,changed=False)
        data['items'].pop(item)
    data['revision']+=1
    db.execute("INSERT INTO inventory_sections VALUES('Kurunegala',?) ON CONFLICT(branch) DO UPDATE SET settings=excluded.settings",(json.dumps(data),))
    details=dict(item=item,before='main' if old else 'running',after=section,comment=comment)
    db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',('SECTION-'+str(data['revision']),now(),'Local manager','inventory_section',json.dumps(details)))
    return dict(ok=True,changed=True,revision=data['revision'])
