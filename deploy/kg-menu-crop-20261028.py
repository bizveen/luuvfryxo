from PIL import Image, ImageDraw, ImageOps
from pathlib import Path
import json,hashlib
src=Path(r'C:\Users\HP\Desktop\Thela\Luuv\Fryxo Orange Cone Chips Menu.png')
out=Path('apps/luuvcrm/deploy/kg-menu-images-20261028');out.mkdir(exist_ok=True)
im=Image.open(src).convert('RGB')
regions={}
for code,box in zip(['CHICKEN','FISH','PRAWN','BEEF','SAUSAGE','MIX'],[(0,192,204,326),(0,315,204,456),(0,442,204,593),(0,583,204,728),(0,715,204,867),(0,852,207,1009)]):
 regions['KG-FRIES-'+code]=box
regions.update({'KG-CHIPS-FISH':(632,205,852,397),'KG-CHIPS-WIENILET':(632,387,852,577),'KG-CHIPS-SPICY':(638,568,849,730)})
for code,box in zip(['BROWN','WHITE','GOLDEN','SALSA','HOT','CHEESE'],[(657,779,762,830),(808,779,916,837),(960,778,1074,835),(657,884,763,943),(809,884,918,942),(951,883,1080,947)]):regions['KG-EXTRA-'+code]=box
drinks=['THAI-BLUEBERRY','THAI-STRAWBERRY','THAI-MANGO','SMOOTHIE-MANGO','SMOOTHIE-BLUEBERRY','SMOOTHIE-PASSION','SMOOTHIE-STRAWBERRY','ICED-COFFEE','ICED-CHOCOLATE','LEMONADE-BLUEBERRY','LEMONADE-STRAWBERRY','LEMONADE-PASSION','LEMON-MINT']
ys=[203,258,309,361,412,466,518,571,623,678,732,790,848,952]
for i,code in enumerate(drinks):regions['KG-'+code]=(1124,ys[i],1188,ys[i+1])
manifest=[]
polygons={
'KG-FRIES-CHICKEN':[(0,240),(26,220),(40,198),(74,204),(111,193),(155,199),(179,218),(202,246),(179,289),(101,326),(25,304)],
'KG-FRIES-FISH':[(0,370),(24,348),(54,330),(94,321),(123,316),(159,333),(181,355),(203,383),(176,422),(101,454),(24,429)],
'KG-FRIES-PRAWN':[(0,501),(16,481),(12,456),(36,469),(62,446),(94,465),(134,446),(165,444),(181,460),(172,483),(204,506),(180,551),(101,590),(25,564)],
'KG-FRIES-BEEF':[(0,632),(24,611),(51,592),(82,600),(116,584),(149,595),(181,617),(203,646),(180,689),(100,725),(24,700)],
'KG-FRIES-SAUSAGE':[(0,769),(24,747),(51,730),(79,732),(109,717),(144,725),(179,748),(202,775),(178,821),(100,864),(25,834)],
'KG-FRIES-MIX':[(0,905),(20,883),(53,869),(78,864),(116,854),(154,866),(181,885),(205,914),(178,964),(99,1005),(24,978)],
'KG-CHIPS-FISH':[(633,254),(650,237),(676,221),(708,217),(734,225),(766,205),(791,215),(812,237),(843,249),(843,274),(750,396),(653,285)],
'KG-CHIPS-WIENILET':[(634,437),(648,417),(665,394),(687,402),(718,392),(751,387),(777,402),(806,415),(842,440),(845,462),(751,574),(653,480)],
'KG-CHIPS-SPICY':[(640,616),(660,595),(684,580),(715,576),(746,568),(776,584),(807,594),(842,617),(837,641),(751,727),(657,651)]}
sheet=Image.new('RGB',(700,((len(regions)+4)//5)*170),'#eeeeee');draw=ImageDraw.Draw(sheet)
for i,(code,box) in enumerate(regions.items()):
 crop=im.crop(box)
 if code in polygons:
  mask=Image.new('L',crop.size,0)
  ImageDraw.Draw(mask).polygon([(x-box[0],y-box[1]) for x,y in polygons[code]],fill=255)
  clean=Image.new('RGB',crop.size,'white');clean.paste(crop,(0,0),mask);crop=clean
 if code=='KG-EXTRA-HOT':crop=crop.crop((0,0,crop.width,54))
 # Remove the printed separators at the outside edge of food crop rectangles.
 if code.startswith('KG-FRIES'):
  d=ImageDraw.Draw(crop)
  for yy in range(crop.height):
   for xx in range(max(0,crop.width-18),crop.width):
    r,g,b=crop.getpixel((xx,yy))
    if r>220 and g<110 and b<80 and (yy<18 or yy>crop.height-18):d.point((xx,yy),fill='white')
 canvas=Image.new('RGB',(320,320),'white')
 fitted=ImageOps.contain(crop,(296,284),Image.Resampling.LANCZOS)
 canvas.paste(fitted,((320-fitted.width)//2,(320-fitted.height)//2))
 name=code.lower()+'-20261028.jpg';path=out/name
 canvas.save(path,quality=88,optimize=True)
 manifest.append({'code':code,'file':name,'md5':hashlib.md5(path.read_bytes()).hexdigest(),'box':box})
 thumb=canvas.resize((130,130));x=(i%5)*140;y=(i//5)*170
 sheet.paste(thumb,(x,y));draw.text((x+2,y+132),code.replace('KG-','')[:21],fill='black')
sheet.save(out/'contact-sheet.jpg')
(out/'manifest.json').write_text(json.dumps(manifest,indent=2))
print('Cropped',len(manifest),'images:',out)
