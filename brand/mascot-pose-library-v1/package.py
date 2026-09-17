"""Validate untouched PNG cutouts and build layout previews, gallery and ZIP."""
from pathlib import Path
import base64,json,subprocess,zipfile
from PIL import Image
BASE=Path(__file__).resolve().parent
ROLES=[
('01-neutral','Neutral','Default identity, quiet introductions','Do not use as evidence of service status'),
('02-present-left','Present left','Character on right; copy on left','Keep clear space beyond extended wing'),
('03-present-right','Present right','Character on left; copy on right','Do not mirror the other file'),
('04-inspect','Inspect / think','Quota explanation, checking, FAQs','Not payment-confirmed or error messaging'),
('05-reminder','Gentle reminder','Low-quota notice, useful heads-up','Not severe outage, fraud warning or blame'),
('06-listening','Listening / help','Setup guidance, group support','Does not imply human or 24/7 live support'),
('07-thank-you','Thank you','Gratitude, welcome-back, appreciation','Not standalone proof of payment success'),
('08-celebrate','Celebrate','Giveaway, launch, real milestone','Not quota warning, outage or failed payment')]
items=[]
for id,title,use,avoid in ROLES:
    p=BASE/'exports'/f'aurix-auri-{id}.png'
    with Image.open(p) as im:
        im.load(); assert im.mode=='RGBA'
        alpha=im.getchannel('A'); assert alpha.getextrema()==(0,255)
        bbox=alpha.getbbox(); w,h=im.size
        visible_bbox=alpha.point(lambda a:255 if a>32 else 0).getbbox()
        assert visible_bbox[0]>0 and visible_bbox[1]>0 and visible_bbox[2]<w and visible_bbox[3]<h
        corners=[alpha.getpixel(xy) for xy in [(0,0),(w-1,0),(0,h-1),(w-1,h-1)]]
        assert corners==[0,0,0,0]
    items.append(dict(id=id,title=title,image=str(p.relative_to(BASE)),use=use,avoid=avoid,width=w,height=h,mode='RGBA',alpha_extrema=[0,255],alpha_bbox=bbox,visible_alpha_gt32_bbox=visible_bbox,alpha_note='Low-alpha generation residue extends beyond the visible silhouette; original pixels preserved.',bytes=p.stat().st_size))
(BASE/'manifest.json').write_text(json.dumps({'status':'static production candidates; not rigged model','items':items},indent=2))
for theme,bg,fg,rule in [('light','#F6F8FC','#071421','#DCE4EE'),('dark','#0D2235','#F6F8FC','#24445B')]:
    blocks=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1120"><rect width="1440" height="1120" fill="{bg}"/>',f'<text x="48" y="53" font-family="Arial" font-size="30" font-weight="bold" fill="{fg}">AuriX / Auri — core pose library</text>']
    for i,item in enumerate(items):
        x=40+(i%4)*350;y=92+(i//4)*492
        data=base64.b64encode((BASE/item['image']).read_bytes()).decode()
        blocks.append(f'<image x="{x+14}" y="{y}" width="310" height="388" href="data:image/png;base64,{data}"/>')
        blocks.append(f'<path d="M{x+10} {y+410}H{x+320}" stroke="{rule}"/>')
        blocks.append(f'<text x="{x+14}" y="{y+447}" font-family="Arial" font-size="23" font-weight="bold" fill="{fg}">{item["title"]}</text>')
    blocks.append('</svg>')
    src=BASE/f'preview-{theme}.svg';src.write_text(''.join(blocks))
    subprocess.run(['rsvg-convert',str(src),'-o',str(BASE/f'preview-{theme}.png')],check=True)
cards=''.join(f'<article><div class="asset"><img src="{a["image"]}" alt="{a["title"]}"></div><h2>{a["title"]}</h2><p>{a["use"]}</p><small>{a["avoid"]}</small><p><a href="{a["image"]}" download>Download transparent PNG</a></p></article>' for a in items)
(BASE/'index.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>AuriX mascot library</title><style>body{background:#f6f8fc;color:#071421;font:16px system-ui;margin:32px}h1{font-size:32px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}article{padding:18px;border:1px solid #dce4ee;border-radius:12px}.asset{background:repeating-conic-gradient(#fff 0 25%,#e9edf1 0 50%) 0/24px 24px;height:340px}img{width:100%;height:100%;object-fit:contain}h2{font-size:20px}small{color:#526273}a{color:#5140ba}</style><h1>Auri / eight reusable poses</h1><p>Transparent 1122 × 1402 PNGs. Static artwork, not a rigged 3D character. Keep the AuriX logo separate.</p><div class="grid">'''+cards+'</div></html>')
with zipfile.ZipFile(BASE/'aurix-auri-pose-library-v1.zip','w',zipfile.ZIP_DEFLATED) as z:
    for p in [BASE/a['image'] for a in items]+[BASE/'README.md',BASE/'PROMPTS.json',BASE/'manifest.json',BASE/'index.html',BASE/'preview-light.png',BASE/'preview-dark.png']:
        z.write(p,str(p.relative_to(BASE)))
print(json.dumps({'pngs':len(items),'alpha_verified':True,'visible_silhouette_not_clipped':True,'low_alpha_residue':'Recorded; not retouched','zip_bytes':(BASE/'aurix-auri-pose-library-v1.zip').stat().st_size}))
