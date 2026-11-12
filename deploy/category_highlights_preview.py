"""Create two isolated local bills to visually compare changed and unchanged rows."""
import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-inventory'))
from server import Store
dest=Path(__file__).resolve().parents[3]/'deploy-state/category-highlights-20261112/review.sqlite3'
assert not dest.exists(),'Preview already exists; reuse it.'
store=Store(dest);store.dispatch('demo_stock',{})
def call(method,**args):
    args.setdefault('_actor','Local manager');return store.dispatch('native:'+method,args)
for vendor in ('Sample bill - unchanged','Sample bill - category corrected'):
    name=call('create_purchase_bill',_actor='Local purchaser',vendor=vendor,lines=json.dumps([
        dict(item='KG-STOCK-HOT',qty=2,price=100,unit='L',category='Food / Ingredients')]))['name']
    call('resolve_approval',name=name,decision='approved')
    if vendor.endswith('corrected'):
        data=call('purchase_categories.review',name=name)
        call('purchase_categories.correct',name=name,expected_revision=data['revision'],request_id=uuid4().hex,
             line_categories=json.dumps([dict(row_name=data['bill']['lines'][0]['row_name'],category='Not sure')]))
print('Ready:',dest)
