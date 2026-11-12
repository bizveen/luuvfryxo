"""Isolated loopback visual review with clearly labelled sample orders; no live connections."""
import copy,io,json,sys
from datetime import date,datetime
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
from jinja2 import Environment,ChoiceLoader,DictLoader,FileSystemLoader
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'local-inventory'))
from server import Store,handler
from waiter_delivery_release import release
store=Store(release.STATE/'waiter-preview.sqlite3')
key='["SAMPLE-FRIES",0]'
def sample(name,table,paid,qty,delivered):
    return dict(name=name,table=table,paid=paid,closed=paid,order_type='Dine In',kitchen_status='Ready',customer='',created=str(date.today())+' 14:20:00',revision=0,complete=delivered==qty,history=[],lines=[dict(key=key,item='SAMPLE-FRIES',name='Crispy Chicken Loaded Fries',qty=qty,delivered=delivered,remaining=qty-delivered,takeaway=0,notes=['No hot sauce · white sauce instead'] if table=='Table 4' else [])])
orders=[sample('SAMPLE-001','Table 4',False,3,1),sample('SAMPLE-002','Table 8',True,2,0),sample('SAMPLE-003','Table 2',False,2,2)]
proofs={};requests={};confirmed={}
def api(method,p):
    if method=='orders':return dict(orders=orders if p.get('day')==str(date.today()) else [],day=p.get('day'),restaurant='Kurunegala')
    o=next((o for o in orders if o['name']==p.get('order_name')),None)
    if not o:raise ValueError('Order not found')
    if method=='upload_photo':
        photo='SAMPLE-PHOTO-'+str(len(proofs)+1);proofs[photo]=dict(order=o['name'],content=p['content']);return dict(photo=photo,url='/preview/photo/'+photo)
    if method=='confirm':
        if p['request_id'] in confirmed:return dict(ok=True,order=o)
        if p['expected_revision']!=o['revision']:raise ValueError('Delivery changed. Refresh this order.')
        for line in p['lines']:
            target=next(l for l in o['lines'] if l['key']==line['key']);qty=float(line['qty'])
            if not qty.is_integer() or not 0<qty<=target['remaining']:raise ValueError('Invalid delivery quantity')
            target['delivered']+=int(qty);target['remaining']-=int(qty)
        o['revision']+=1;o['complete']=all(l['remaining']==0 for l in o['lines']);confirmed[p['request_id']]=True
        o['history'].append(dict(kind='confirmed',by_name='Sample waiter',when=str(datetime.now()),lines=[dict(name=l['name'],qty=p['lines'][0]['qty']) for l in o['lines']]))
        return dict(ok=True,order=o)
    if method=='request_removal':
        if o['paid'] or o['closed']:raise ValueError('Open unpaid bills only')
        if len(p.get('comment','').strip())<3 or p.get('photo') not in proofs:raise ValueError('Comment and photo required')
        if p['expected_revision']!=o['revision']:raise ValueError('Delivery changed')
        name='SAMPLE-APPROVAL-'+str(len(requests)+1)
        requests[name]=dict(name=name,type='edit',pos_order=o['name'],status='pending',title='Delivered item removal',subhead=o['table'],by='Sample waiter',created_when=str(datetime.now()),time='Just now',resolved_by='',resolved_when='',mine=False,self_ok=True,delivery_removal=True,delivery_comment=p['comment'],image='/preview/photo/'+p['photo'],manager_comment='',lines=[dict(name='Crispy Chicken Loaded Fries',qtyStr=str(p['qty'])+' delivered',totalStr='')],qty=p['qty'],key=p['key'])
        return dict(ok=True,name=name)
    raise ValueError('Unsupported preview action')
class Handler(handler(store)):
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/waiter2.0':
            env=Environment(loader=ChoiceLoader([DictLoader({'templates/includes/branch_switcher.html':'','templates/includes/app_switch.html':''}),FileSystemLoader(str(ROOT/'luuvcrm'))]),autoescape=True)
            html=env.get_template('www/waiter_v2.html').render(csrf_token='preview',restaurant_name='Kurunegala · SAMPLE DATA',current_user_full='Sample waiter',today=str(date.today()))
            html=html.replace('<body>','<body><div style="padding:10px 20px;background:#302820;color:#fff;font:12px system-ui;">LOCAL REVIEW · Sample orders · Live bills unchanged · <a style="color:#ffb995;" href="/manager2.0?tab=approvals">Manager approvals</a></div>')
            self.send(200,html,'text/html');return
        if path.startswith('/preview/photo/'):
            import base64
            raw=base64.b64decode(proofs[path.rsplit('/',1)[1]]['content']);self.send_response(200);self.send_header('Content-Type','image/jpeg');self.end_headers();self.wfile.write(raw);return
        super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path;prefix='/api/method/luuvcrm.api.';method=path.removeprefix(prefix)
        if method not in ('get_pending_approvals','resolve_approval','removal_report.history') and not method.startswith('waiter_delivery.'):
            return super().do_POST()
        if self.headers.get('Origin') and urlparse(self.headers['Origin']).netloc!=self.headers.get('Host'):self.send(403,'{}');return
        size=int(self.headers.get('Content-Length','0'))
        if size>7*1024*1024:self.send(413,'{}');return
        raw=self.rfile.read(size)
        p=json.loads(raw) if 'application/json' in self.headers.get('Content-Type','') else {k:v[-1] for k,v in parse_qs(raw.decode()).items()}
        try:
            if method.startswith('waiter_delivery.'):result=api(method.split('.')[-1],p)
            elif method=='get_pending_approvals':result=dict(approvals=list(requests.values()),pending=sum(r['status']=='pending' for r in requests.values()))
            elif method=='removal_report.history':
                rows=list(requests.values());result=dict(rows=rows,total=len(rows),page=0,page_size=50,counts={s:sum(r['status']==s for r in rows) for s in ('pending','approved','rejected','superseded')});result['counts']['all']=len(rows)
            elif p.get('name') in requests:
                a=requests[p['name']]
                if len(p.get('comment','').strip())<3:raise ValueError('Add a manager comment')
                if a['status']!='pending':raise ValueError('Already resolved')
                a.update(status=p['decision'],manager_comment=p['comment'],resolved_by='Local manager',resolved_when=str(datetime.now()))
                if p['decision']=='approved':
                    o=next(o for o in orders if o['name']==a['pos_order']);l=o['lines'][0];l['qty']-=a['qty'];l['delivered']=min(l['qty'],l['delivered']);l['remaining']=l['qty']-l['delivered'];o['complete']=l['remaining']==0;o['revision']+=1
                    o['history'].append(dict(kind='removed',by_name='Local manager',when=str(datetime.now()),lines=[dict(name=l['name'],qty=a['qty'])],comment=a['delivery_comment'],manager_comment=a['manager_comment'],photo=a['image']))
                result=dict(ok=True,executed='items_updated')
            else:self.rfile=io.BytesIO(raw);return super().do_POST()
            self.send(200,json.dumps(dict(message=result)))
        except (ValueError,KeyError) as e:self.send(400,json.dumps(dict(error=str(e))))
if __name__=='__main__':
    print('Waiter sample review: http://127.0.0.1:4194/waiter2.0',flush=True)
    ThreadingHTTPServer(('127.0.0.1',4194),Handler).serve_forever()
