"""Bangkok edition; preserves approved base artwork and public-claim mechanics."""
from pathlib import Path
import base64,subprocess,sys,json
BASE=Path(__file__).resolve().parent
V4=BASE.parent/'free-key-drop-v4'
STAMP='20260907-050529'
def render(r=2):
 source=V4/'exports/20260907-042225_aurix-free-key-drop-v4_r2.svg'
 if not source.exists():subprocess.run([sys.executable,str(V4/'render.py'),'2'],check=True)
 svg=source.read_text()
 flag=base64.b64encode((BASE/'flag-th.svg').read_bytes()).decode()
 badge=f'''<g aria-label="Thailand Bangkok server location"><rect x="605" y="482" width="130" height="90" rx="4" fill="#F8FBFC"/><image href="data:image/svg+xml;base64,{flag}" x="610" y="487" width="120" height="80"/></g>'''
 marker='<text x="540" y="639"';assert marker in svg
 svg=svg.replace(marker,badge+marker,1)
 label=f'<text x="540" y="694" text-anchor="middle" font-family="Inter" font-size="{[28,30,30][r]}" font-weight="600" letter-spacing="{[4,3,2][r]}" fill="#B7CBD8">THAILAND · BKK</text>'
 svg=svg.replace('<text x="540" y="794"',label+'<text x="540" y="794"',1)
 out=BASE/('exports' if r==2 else 'iterations');out.mkdir(exist_ok=True)
 stem=f'{STAMP}_aurix-bkk-key-drop_r{r}'
 p=out/(stem+'.svg');p.write_text(svg)
 for suffix,w,h in [('',1080,1350),('_preview',324,405)]:
  subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(out/(stem+suffix+'.png')),str(p)],check=True)
 if r==2:
  caption=source.with_suffix('.txt').read_text().replace('Outline VPN Key အခမဲ့လိုချင်ရင်','Thailand (BKK) Outline VPN Key အခမဲ့လိုချင်ရင်',1).replace('AuriX က VPN Quota','AuriX က Bangkok (BKK) Server ပေါ်မှာ VPN Quota',1)
  (out/(stem+'.txt')).write_text(caption)
  manifest={'campaign':'aurix-bkk-key-drop','stamp':STAMP,'format':'1080x1350 PNG + TXT + SVG','recommended_order':['bkk-key-drop'],'finals':[{'id':'bkk-key-drop','role':'reach','image':stem+'.png','caption':stem+'.txt','source':stem+'.svg'}],'preview':stem+'_preview.png','publication_gates':['Bangkok location user-provided. Verify five keys, unlimited quota and seven-day activation/expiry before publication.','Post actual keys with expiry information. Public checkmarks cannot prevent reuse.']}
  (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
if __name__=='__main__':render(int(sys.argv[1]) if len(sys.argv)>1 else 2)
