import frappe,json,pathlib,hashlib,urllib.request
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
try:
 from luuvcrm import api
 def state():return json.dumps({'menu':api.menu_groups('Luuv Fryxo'),'doc':frappe.get_doc('Restaurant Menu','Luuv Fryxo-Main-Menu').as_dict(),'prices':frappe.get_all('Item Price',fields=['*'],order_by='name',limit_page_length=0),'profiles':frappe.get_all('POS Profile',fields=['*'],order_by='name',limit_page_length=0)},default=str,sort_keys=True)
 before=state()
 items=api.menu_items('Kurunegala');assert len(items)==28
 rates={r['code']:r['rate'] for r in items}
 backup=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/kg-menu-images-before-20261028.json')
 assert not backup.exists()
 backup.write_text(json.dumps({'items':items,'other':json.loads(before)},default=str,indent=2))
 for row in items:
  code=row['code'];assert code.startswith('KG-')
  assert not frappe.db.exists('Restaurant Menu Item',{'parent':['!=','Kurunegala-Main-Menu'],'item':code})
  filename=code.lower()+'-20261028.jpg'
  assert pathlib.Path(frappe.get_site_path('public','files',filename)).exists()
  values={'image':'/files/'+filename}
  if code=='KG-CHIPS-WIENILET':
   values.update(item_name='Crispy Wings and Chips',description='Crispy chicken wings with crunchy potato chips, mixed garden creamy salad, mozzarella cheese, Luuv Chef special Hot Sauce and Smoky Brown Sauce.')
  frappe.db.set_value('Item',code,values)
 assert state()==before
 updated=api.menu_items('Kurunegala')
 assert {r['code']:r['rate'] for r in updated}==rates
 assert all(r['image'] for r in updated)
 assert next(r for r in updated if r['code']=='KG-CHIPS-WIENILET')['name']=='Crispy Wings and Chips'
 frappe.db.commit();frappe.clear_cache()
 from frappe.website.utils import clear_website_cache
 clear_website_cache()
 print('SAVED: 28 images, Crispy Wings and Chips name, Dambulla and all prices/profiles unchanged.')
 with urllib.request.urlopen('https://luuvgrand.com/menu?r=Kurunegala',timeout=30) as res:
  html=res.read().decode();assert res.status==200
  assert 'Crispy Wings and Chips' in html and 'Crispy Wienilet and Chips' not in html
  for row in updated:assert row['image'] in html
 for row in updated:
  with urllib.request.urlopen('https://luuvgrand.com'+row['image'],timeout=30) as res:
   data=res.read();local=pathlib.Path(frappe.get_site_path('public',row['image'].lstrip('/'))).read_bytes()
   assert res.status==200 and hashlib.md5(data).digest()==hashlib.md5(local).digest()
 print('PUBLIC VERIFY PASS: menu label and all 28 image URLs; image bytes match.')
except Exception:frappe.db.rollback();raise
finally:frappe.destroy()
