import frappe,json,pathlib,hashlib,urllib.request
frappe.init(site='fryxo');frappe.connect()
try:
 from luuvcrm import api
 backup=json.loads(pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/kg-menu-before-20261028.json').read_text())
 expected={r['code']:r['rate'] for r in backup['planned']}
 assert frappe.db.get_value('Restaurant','Kurunegala','active_menu')=='Kurunegala-Main-Menu'
 assert {r['code']:r['rate'] for r in api.menu_items('Kurunegala')}==expected
 before=backup['before']
 def norm(x):return json.loads(json.dumps(x,default=str))
 assert norm(frappe.get_doc('Restaurant','Luuv Fryxo').as_dict())==before['restaurant']
 assert norm(frappe.get_doc('Restaurant Menu','Luuv Fryxo-Main-Menu').as_dict())==before['menu']
 assert norm(api.menu_groups('Luuv Fryxo'))==before['public_menu']
 assert norm(frappe.get_all('Item Price',filters={'price_list':['!=','Kurunegala-Main-Menu']},fields=['*'],order_by='name',limit_page_length=0))==before['prices']
 assert norm(frappe.get_all('POS Profile',fields=['*'],order_by='name',limit_page_length=0))==before['profiles']
 for branch in ['Kurunegala','Luuv%20Fryxo']:
  url='https://luuvgrand.com/menu?r='+branch
  with urllib.request.urlopen(url,timeout=30) as response:
   html=response.read().decode()
   assert response.status==200
   if branch=='Kurunegala':
    for row in backup['planned']:assert row['name'] in html, row['name']
   else:assert 'Crispy Wienilet and Chips' not in html and 'Thai Milk Iced Tea with Blueberry' not in html
   print('PUBLIC MENU PASS',url)
 print('PASS: 46 exact menu prices; Dambulla restaurant/menu/API/prices and all POS profiles unchanged.')
finally:frappe.db.rollback();frappe.destroy()
