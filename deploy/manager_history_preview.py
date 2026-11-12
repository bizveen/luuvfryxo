"""Isolated sample data for category defaults and approval-history UI review."""
import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-inventory'))
from server import Store,now
dest=Path(__file__).resolve().parents[3]/'deploy-state/manager-history-20261112/review.sqlite3'
assert not dest.exists(),'Preview already exists; reuse it.'
store=Store(dest);store.dispatch('demo_stock',{})
def call(method,**args):
    args.setdefault('_actor','Local manager');return store.dispatch('native:'+method,args)
name=call('create_purchase_bill',_actor='Local purchaser',vendor='Sample supplier',lines=json.dumps([
    dict(item='KG-STOCK-HOT',qty=2,price=100,unit='L',category='Food / Ingredients')]))['name']
call('resolve_approval',name=name,decision='approved');data=call('purchase_categories.review',name=name)
call('purchase_categories.correct',name=name,expected_revision=data['revision'],request_id=uuid4().hex,
     line_categories=json.dumps([dict(row_name=data['bill']['lines'][0]['row_name'],category='Staff Meals')]))
with store.connect() as db:
    for i,status in enumerate(('approved','rejected','pending','superseded')):
        data=dict(name='SAMPLE-REMOVAL-'+str(i),pos_order='SAMPLE-POS-010'+str(i),type='edit',status=status,
            title='Remove item approval',subhead='Sample bill',by='Sample cashier',requested_at=now(),
            created_when='12 Nov 2026, 1:45 pm',time='Just now',resolved_by='Sample manager' if status!='pending' else '',
            resolved_when='12 Nov 2026, 1:50 pm' if status!='pending' else '',
            lines=[dict(name='Crispy Chicken Loaded Fries',qtyStr='1 ×',totalStr='Rs 1,500')],
            manager_comment='Customer changed their order' if status!='pending' else '',flag_text='Item already sent to kitchen')
        db.execute('INSERT INTO removal_approval_samples VALUES(?,?)',(data['name'],json.dumps(data)))
print('Ready:',dest)
