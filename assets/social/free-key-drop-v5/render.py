"""Non-destructive Singapore edition of the V4 poster."""
from pathlib import Path
import base64,subprocess,sys,json
BASE=Path(__file__).resolve().parent
V4=BASE.parent/'free-key-drop-v4'
STAMP='20260907-050319'
def render(r=2):
 source=V4/'exports/20260907-042225_aurix-free-key-drop-v4_r2.svg'
 if not source.exists():subprocess.run([sys.executable,str(V4/'render.py'),'2'],check=True)
 svg=source.read_text()
 flag=base64.b64encode((BASE/'flag-sg.svg').read_bytes()).decode()
 x,y,w,h=[(623,492,96,64),(610,487,120,80),(610,487,120,80)][r]
 badge=f'''<g aria-label="Singapore server location">
 <rect x="{x-5}" y="{y-5}" width="{w+10}" height="{h+10}" rx="4" fill="#F8FBFC"/>
 <image href="data:image/svg+xml;base64,{flag}" x="{x}" y="{y}" width="{w}" height="{h}"/>
 </g>'''
 marker='<text x="540" y="639"'
 assert marker in svg
 svg=svg.replace(marker,badge+marker,1)
 label=f'<text x="540" y="694" text-anchor="middle" font-family="Inter" font-size="{[25,25,30][r]}" font-weight="600" letter-spacing="4" fill="#B7CBD8">SINGAPORE</text>'
 svg=svg.replace('<text x="540" y="794"',label+'<text x="540" y="794"',1)
 out=BASE/('exports' if r==2 else 'iterations');out.mkdir(exist_ok=True)
 stem=f'{STAMP}_aurix-singapore-key-drop-v5_r{r}'
 p=out/(stem+'.svg');p.write_text(svg)
 for suffix,w,h in [('',1080,1350),('_preview',324,405)]:
  subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(out/(stem+suffix+'.png')),str(p)],check=True)
 if r==2:
  caption=source.with_suffix('.txt').read_text().replace('Outline VPN Key အခမဲ့လိုချင်ရင်','Singapore Outline VPN Key အခမဲ့လိုချင်ရင်',1).replace('AuriX က VPN Quota','AuriX က Singapore Server ပေါ်မှာ VPN Quota',1)
  (out/(stem+'.txt')).write_text(caption)
  manifest={'campaign':'aurix-singapore-key-drop-v5','stamp':STAMP,'format':'1080x1350 PNG + TXT + SVG','recommended_order':['singapore-key-drop'],'finals':[{'id':'singapore-key-drop','role':'reach','image':stem+'.png','caption':stem+'.txt','source':stem+'.svg'}],'preview':stem+'_preview.png','publication_gates':['Singapore location is user-provided; verify five keys, unlimited quota and activation/expiry before publishing.','Public checkmarks cannot prevent reuse. Post keys alongside caption.']}
  (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
if __name__=='__main__':render(int(sys.argv[1]) if len(sys.argv)>1 else 2)
