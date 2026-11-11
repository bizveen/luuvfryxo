"""Requested local example stock. Never connects to the live POS."""
from pathlib import Path
from server import Store

SAMPLES = [
    ('KG-STOCK-POTATO',8.4,3,700),('KG-STOCK-CHICKEN',6.25,2,1800),
    ('KG-STOCK-SALAD',1.2,1.5,500),('KG-STOCK-WHITE',2.8,1,900),
    ('KG-STOCK-SPECIAL',1.6,1,1100),('KG-STOCK-HOT',0.6,1,800),
    ('KG-STOCK-MOZZARELLA',0.9,1,3500),('KG-STOCK-OIL',4.5,2,1000),
    ('KG-STOCK-FLOUR',5.2,2,300),('KG-STOCK-BOX',120,30,45),
    ('KG-STOCK-CUP',18,30,8),('KG-STOCK-FORK',75,25,5),
]

def seed(store):
    with store.connect() as db:
        db.executemany('INSERT OR IGNORE INTO stock(item,name,stock_uom,uom,balance,rate) VALUES(?,?,\'nos\',\'nos\',0,0)',[
            ('KG-STOCK-BOX','Loaded fries boxes'),('KG-STOCK-CUP','Sauce cups'),('KG-STOCK-FORK','Wooden forks')])
    added=[]
    for item,quantity,minimum,rate in SAMPLES:
        existing=next(r for r in store.dispatch('dashboard',{})['stock'] if r['item']==item)
        # Preserve any quantities already entered during the user's review.
        if existing['balance']==0:
            store.dispatch('correct_stock',dict(item_code=item,counted_qty=quantity,expected_qty=0,
                valuation_rate=rate,comment='Sample quantity and price for localhost visual review only; not an actual branch count.'))
            added.append(item)
        if existing['minimum_qty']==0:
            store.dispatch('set_minimum',dict(item_code=item,minimum_qty=minimum))
    return added

if __name__=='__main__':
    store=Store(Path(__file__).parent/'data/preview.sqlite3')
    print('Added local sample quantities:',len(seed(store)))
