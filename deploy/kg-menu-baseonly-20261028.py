import frappe,json,pathlib,urllib.request
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
MENU='Kurunegala-Main-Menu'
def norm(x):return json.loads(json.dumps(x,default=str))
def other_state():
 return norm({'restaurant':frappe.get_doc('Restaurant','Luuv Fryxo').as_dict(),
 'menu':frappe.get_doc('Restaurant Menu','Luuv Fryxo-Main-Menu').as_dict(),
 'prices':frappe.get_all('Item Price',filters={'price_list':['!=',MENU]},fields=['*'],order_by='name',limit_page_length=0),
 'profiles':frappe.get_all('POS Profile',fields=['*'],order_by='name',limit_page_length=0),
 'public_menu':api.menu_groups('Luuv Fryxo')})
try:
 from luuvcrm import api
 menu=frappe.get_doc('Restaurant Menu',MENU)
 assert menu.restaurant=='Kurunegala' and menu.price_list==MENU
 assert frappe.db.get_value('Restaurant','Kurunegala','active_menu')==MENU
 before=other_state()
 original=api.menu_items('Kurunegala')
 remove={'KG-FRIES-'+base+suffix for base in ['CHICKEN','FISH','PRAWN','BEEF','SAUSAGE','MIX'] for suffix in ['-MUSTARD','-SALSA','-CHEESE']}
 assert len(original)==46 and remove.issubset({r['code'] for r in original})
 expected={r['code']:r['rate'] for r in original if r['code'] not in remove}
 backup=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/kg-menu-baseonly-before-20261028.json')
 if backup.exists():
  assert json.loads(backup.read_text())['menu']==norm(menu.as_dict()),'Menu changed since backed-up attempt'
 else:backup.write_text(json.dumps({'menu':menu.as_dict(),'other':before,'expected':expected},default=str,indent=2))
 # Existing Hospitality save hook attempts to recreate the existing price list.
 # Remove only the specified child rows and matching branch prices atomically.
 frappe.db.delete('Restaurant Menu Item',{'parent':MENU,'parenttype':'Restaurant Menu','item':['in',sorted(remove)]})
 frappe.db.delete('Item Price',{'price_list':MENU,'item_code':['in',sorted(remove)]})
 menu.reload()
 for idx,row in enumerate(menu.items,1):frappe.db.set_value('Restaurant Menu Item',row.name,'idx',idx,update_modified=False)
 frappe.db.set_value('Restaurant Menu',MENU,'modified',frappe.utils.now())
 assert len(menu.items)==28
 assert {r['code']:r['rate'] for r in api.menu_items('Kurunegala')}==expected
 assert {r.item_code:r.price_list_rate for r in frappe.get_all('Item Price',filters={'price_list':MENU},fields=['item_code','price_list_rate'])}==expected
 assert other_state()==before
 frappe.db.commit()
 frappe.clear_cache()
 from frappe.website.utils import clear_website_cache
 clear_website_cache()
 print('SAVED: removed 18 option rows; retained 6 base fries, 3 chips, 13 drinks, 6 add-ons. Dambulla unchanged.')
 with urllib.request.urlopen('https://luuvgrand.com/menu?r=Kurunegala',timeout=30) as response:
  html=response.read().decode()
  assert response.status==200
  for row in original:
   if row['code'] in remove:assert row['name'] not in html,row['name']
   else:assert row['name'] in html,row['name']
 print('PUBLIC MENU PASS: all 28 retained items present; all 18 option names absent.')
except Exception:frappe.db.rollback();raise
finally:frappe.destroy()
