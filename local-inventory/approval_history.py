"""Read-only local approval report; sample removals live only in the preview DB."""
import json
from collections import Counter
from datetime import date,timedelta

def initialize(db):db.execute('CREATE TABLE IF NOT EXISTS removal_approval_samples(name TEXT PRIMARY KEY,data TEXT NOT NULL)')

def history(db,p,actor):
    from existing_purchases import approvals
    if actor!='Local manager':raise ValueError('Only a manager can view approval history.')
    end=date.fromisoformat(p.get('end') or date.today().isoformat())
    start=date.fromisoformat(p.get('start') or (end-timedelta(days=29)).isoformat())
    if start>end or (end-start).days>365:raise ValueError('Choose a date range of up to one year.')
    kind=p.get('kind','removals');status=p.get('status','all');page=max(0,int(p.get('page') or 0))
    if kind not in ('all','removals','bill_changes') or status not in ('all','pending','approved','rejected','superseded'):raise ValueError('Choose a valid history filter.')
    rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM removal_approval_samples')]
    if kind=='all':rows+=approvals(db,actor)
    rows=[r for r in rows if str(start)<=r['requested_at'][:10]<=str(end)]
    rows.sort(key=lambda r:r['requested_at'],reverse=True)
    counts=dict(Counter(r['status'] for r in rows));counts['all']=len(rows)
    if status!='all':rows=[r for r in rows if r['status']==status]
    return dict(rows=rows[page*50:(page+1)*50],counts=counts,total=len(rows),page=page,page_size=50,start=str(start),end=str(end))
