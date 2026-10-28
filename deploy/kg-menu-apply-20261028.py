import frappe,json,pathlib,hashlib,os
frappe.init(site='fryxo');frappe.connect();frappe.set_user('Administrator')
MENU='Kurunegala-Main-Menu'
def snapshot():
 return { 'restaurant':frappe.get_doc('Restaurant','Luuv Fryxo').as_dict(),
  'menu':frappe.get_doc('Restaurant Menu','Luuv Fryxo-Main-Menu').as_dict(),
  'prices':frappe.get_all('Item Price',filters={'price_list':['!=',MENU]},fields=['*'],order_by='name',limit_page_length=0),
  'profiles':frappe.get_all('POS Profile',fields=['*'],order_by='name',limit_page_length=0),
  'public_menu':api.menu_groups('Luuv Fryxo') }
def digest(obj): return hashlib.sha256(json.dumps(obj,default=str,sort_keys=True).encode()).hexdigest()
try:
 from luuvcrm import api
 restaurant=frappe.get_doc('Restaurant','Kurunegala')
 assert not restaurant.active_menu
 assert not frappe.db.exists('Restaurant Menu',MENU)
 assert not frappe.db.exists('Price List',MENU)
 assert frappe.db.get_value('POS Profile','Kurunegala','restaurant')=='Kurunegala'
 before=snapshot()
 rows=[]
 def add(code,name,group,rate,description=''):
  rows.append(dict(code='KG-'+code,name=name,group=group,rate=rate,description=description or name))
 fries=[
  ('CHICKEN','Crispy Chicken Loaded Fries',1890,'Crispy chicken, creamy coleslaw, mozzarella cheese, hot sauce, garlic mayo, mustard mayo and BBQ sauce.'),
  ('FISH','Crispy Fish Loaded Fries',2290,'Crispy fish, creamy coleslaw, mozzarella cheese, hot sauce, garlic mayo, mustard mayo and BBQ sauce.'),
  ('PRAWN','Tempura Prawn Loaded Fries',2590,'Tempura prawns, creamy coleslaw, mozzarella cheese, hot sauce, BBQ sauce, garlic mayo and mustard mayo.'),
  ('BEEF','Ground Beef Spicy Masala Loaded Fries',2290,'Spicy masala ground beef, garden mayo salad, mozzarella cheese, hot sauce, BBQ sauce, garlic mayo and mustard mayo.'),
  ('SAUSAGE','Grilled Bratwurst Sausage Loaded Fries',1690,'Grilled bratwurst sausage, creamy salad, mozzarella cheese, BBQ sauce, mustard mayo, garlic mayo and hot sauce.'),
  ('MIX','Mix Meat Loaded Fries',3590,'Crispy chicken, prawns and sausage with golden fries, creamy coleslaw, mozzarella cheese and signature sauces.')]
 for code,name,rate,desc in fries:
  for suffix,label,extra in [('', '',0),('-MUSTARD',' + Mustard Mayo',100),('-SALSA',' + Tomato Salsa',100),('-CHEESE',' + Cheese',200)]:
   add('FRIES-'+code+suffix,name+label,'Loaded Fries',rate+extra,desc+(' Extra'+label+'.' if label else ''))
 add('CHIPS-FISH','Fish and Chips','Chips',990,'Crispy fish with crunchy chips, mixed garden creamy salad, mozzarella cheese, hot sauce and BBQ sauce.')
 add('CHIPS-WIENILET','Crispy Wienilet and Chips','Chips',790,'Crispy wienilet with crunchy chips, mixed garden creamy salad, mozzarella cheese, hot sauce and BBQ sauce.')
 add('CHIPS-SPICY','Spicy Chips','Chips',490,'Crispy chips tossed with spicy seasoning, served with creamy salad and BBQ & hot sauce.')
 drinks=[('THAI-BLUEBERRY','Thai Milk Iced Tea with Blueberry',700,990),('THAI-STRAWBERRY','Thai Tea with Strawberry',700,790),('THAI-MANGO','Thai Tea with Mango',700,790),('SMOOTHIE-MANGO','Mango Smoothie',700,890),('SMOOTHIE-BLUEBERRY','Blueberry Smoothie',700,1090),('SMOOTHIE-PASSION','Passion Smoothie',700,890),('SMOOTHIE-STRAWBERRY','Strawberry Smoothie',700,890),('ICED-COFFEE','Iced Coffee',700,990),('ICED-CHOCOLATE','Iced Chocolate',700,990),('LEMONADE-BLUEBERRY','Blueberry Lemonade',300,590),('LEMONADE-STRAWBERRY','Strawberry Lemonade',300,590),('LEMONADE-PASSION','Passion Lemonade',300,490),('LEMON-MINT','Lemon and Mint',300,490)]
 for code,name,ml,rate in drinks: add(code,name+' ('+str(ml)+'ml)','Drinks',rate)
 for code,name,rate in [('BROWN','Smoky Brown Sauce',100),('WHITE','White Sauce',100),('GOLDEN','Golden Sauce',100),('SALSA','Tomato Salsa',100),('HOT','Luuv Chef Special Hot Sauce',100),('CHEESE','Cheese Slices',200)]:
  add('EXTRA-'+code,name,'Add-ons',rate)
 assert len(rows)==46
 assert not any(frappe.db.exists('Item',r['code']) for r in rows)
 backup=pathlib.Path('/home/frappe/frappe-bench/sites/fryxo/private/backups/kg-menu-before-20261028.json')
 if not globals().get('DRY_RUN'):
  assert not backup.exists()
  backup.write_text(json.dumps({'before':before,'kurunegala':restaurant.as_dict(),'planned':rows},default=str,indent=2))
 for group in ['Loaded Fries','Chips','Drinks']:
  if not frappe.db.exists('Item Group',group):
   frappe.get_doc(dict(doctype='Item Group',item_group_name=group,parent_item_group='All Item Groups',is_group=0)).insert()
 for r in rows:
  kitchen=frappe.db.get_value('Kitchen Group',{'group_name':'Beverages' if r['group']=='Drinks' else 'Sides & Starters'},'name')
  frappe.get_doc(dict(doctype='Item',item_code=r['code'],item_name=r['name'],item_group=r['group'],stock_uom='Nos',is_stock_item=0,is_sales_item=1,is_purchase_item=0,standard_rate=0,description=r['description'],kitchen_group=kitchen,item_defaults=[dict(company=restaurant.company,default_warehouse=restaurant.warehouse,selling_cost_center=restaurant.cost_center,income_account='luuv sale - LF')])).insert()
 menu=frappe.get_doc(dict(doctype='Restaurant Menu',restaurant='Kurunegala',enabled=1,items=[dict(item=r['code'],rate=r['rate'],available=1) for r in rows])).insert(set_name=MENU)
 restaurant.active_menu=MENU;restaurant.save()
 assert menu.price_list==MENU
 expected={r['code']:r['rate'] for r in rows}
 assert {r['code']:r['rate'] for r in api.menu_items('Kurunegala')}==expected
 assert {r.item_code:r.price_list_rate for r in frappe.get_all('Item Price',filters={'price_list':MENU},fields=['item_code','price_list_rate'])}==expected
 assert digest(snapshot())==digest(before),'Dambulla or other prices/profile changed'
 report=dict(menu=MENU,count=len(rows),categories={g:sum(r['group']==g for r in rows) for g in sorted(set(r['group'] for r in rows))},dambulla_unchanged=True,dambulla_sha256=digest(before),items=rows)
 if globals().get('DRY_RUN'):
  frappe.db.rollback();print('DRY RUN PASS',json.dumps(report))
 else:
  frappe.db.commit()
  frappe.clear_cache()
  from frappe.website.utils import clear_website_cache
  clear_website_cache()
  print('APPLIED',json.dumps(report))
except Exception:
 frappe.db.rollback();raise
finally: frappe.destroy()
