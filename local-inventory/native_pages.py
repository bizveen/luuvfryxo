"""Render the real app templates with an explicit local-only role switch."""
from pathlib import Path
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
NAV = '''<style>.preview-bar{padding:10px 18px;background:#302b26;color:white;font:12px system-ui;display:flex;gap:12px;flex-wrap:wrap;align-items:center}.preview-bar a{color:#ffd0ac}.preview-bar b{color:#ffbc81}</style><div class="preview-bar"><b>LOCAL REVIEW</b><span>Sample data · live stock unchanged</span><a href="/manager2.0?tab=inventory">Manager 2.0 inventory</a><a href="/manager2.0?tab=approvals">Manager 2.0 approvals</a><a href="/purchase2.0?inventory=1&restaurant=Kurunegala">Purchase 2.0</a><a href="/manager">Original Manager</a><a href="/purchases">Original Purchases</a><a href="/pos">Test POS</a><span>__ACTOR__</span></div>'''

PAGES = {'/': 'manager_v2', '/manager2.0': 'manager_v2',
         '/purchase2.0': 'purchase_v2', '/manager': 'manager', '/purchases': 'purchases'}


def render(page):
    overrides = {'templates/includes/branch_switcher.html':'',
                 'templates/includes/bill_edit.html':'',
                 'templates/includes/app_switch.html':''}
    env = Environment(loader=ChoiceLoader([DictLoader(overrides),FileSystemLoader(str(ROOT/'luuvcrm'))]),autoescape=True)
    actor='Local manager' if page in ('manager','manager_v2') else 'Local purchaser'
    html = env.get_template('www/'+page+'.html').render(brand_name='Luuv Fryxo',csrf_token='local-preview',
        current_user=actor,current_user_full=actor,restaurant_name='Kurunegala · local review',can_edit_bills=False,
        brand=lambda:dict(brand_color='#F15207',brand_color_dark='#D84804',brand_icon_192='/favicon.ico'))
    html=html.replace('<body>','<body>'+NAV.replace('__ACTOR__',actor),1)
    # External assets are decorative. Service workers and error telemetry are
    # deliberately omitted from the loopback harness; real templates retain them.
    app='manager' if page in ('manager','manager_v2') else 'purchases'
    html=html.replace('<script src="/lferr.js?v=1" data-app="'+app+'"></script>','')
    html='\n'.join(line for line in html.split('\n') if 'navigator.serviceWorker.register(' not in line)
    if page=='manager_v2':
        html=html.replace('</body>',"<script>setTab(new URLSearchParams(location.search).get('tab')==='approvals'?'approvals':'inventory');</script></body>")
    return html
