"""Local display-name edits with immutable historical snapshots."""
import json
from uuid import uuid4


def rename(db,p):
    from server import now
    if p.get('_actor') not in (None,'Local manager'):raise ValueError('Only a manager can edit item names.')
    name=p.get('item_name')
    if not isinstance(name,str):raise ValueError('Enter an item name.')
    name=name.strip()
    if not 2<=len(name)<=140 or any(ord(c)<32 or ord(c)==127 or c in '<>' for c in name):
        raise ValueError('Use a plain-text item name of 2–140 characters.')
    item=p.get('item_code');row=db.execute('SELECT * FROM stock WHERE item=?',(item,)).fetchone()
    if not row:raise ValueError('Choose an item in this branch inventory.')
    if name==row['name']:return dict(ok=True,changed=False,item=item,name=name)
    if p.get('expected_name')!=row['name']:raise ValueError('This item name changed on another screen. Reopen it before saving.')
    if db.execute('SELECT 1 FROM stock WHERE lower(name)=lower(?) AND item<>?',(name,item)).fetchone():
        raise ValueError('Another item already uses that name. Choose a different name.')
    db.execute('UPDATE stock SET name=? WHERE item=?',(name,item))
    db.execute('INSERT INTO movements VALUES(?,?,?,?,?)',('ITEM-NAME-'+uuid4().hex,now(),'Local manager','item_name',
        json.dumps(dict(item=item,before=row['name'],after=name))))
    return dict(ok=True,changed=True,item=item,name=name)
