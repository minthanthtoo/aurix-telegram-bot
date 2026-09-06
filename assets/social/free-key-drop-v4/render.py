"""Minimal offer poster: infinity ribbon, authentic marks, four copy beats."""
from pathlib import Path
import base64,subprocess,sys
from xml.sax.saxutils import escape
ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parent
STAMP='20260907-042225'
def img(p,x,y,w,h):
 m='image/svg+xml' if p.suffix=='.svg' else 'image/png'
 return f'<image href="data:{m};base64,{base64.b64encode(p.read_bytes()).decode()}" x="{x}" y="{y}" width="{w}" height="{h}"/>'
def text(y,s,size,color='#F8FBFC',family='Noto Sans Myanmar',group='copy'):
 return f'<text x="540" y="{y}" text-anchor="middle" font-family="{family}" font-size="{size}" font-weight="800" fill="{color}" xml:lang="my" data-mm-role="headline" data-mm-group="{group}" style="font-kerning:normal;font-feature-settings:\'mark\' 1,\'mkmk\' 1">{escape(s)}</text>'
def render(r):
 out=BASE/('exports' if r==2 else 'iterations');out.mkdir(exist_ok=True)
 stem=f'{STAMP}_aurix-free-key-drop-v4_r{r}'
 ribbon='M540 420C440 285 216 255 192 416C166 590 406 607 540 420C674 233 914 250 888 424C864 585 640 555 540 420'
 svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
<defs><linearGradient id="ribbon" x1="160" y1="340" x2="920" y2="540" gradientUnits="userSpaceOnUse"><stop stop-color="#36DEEF"/><stop offset=".5" stop-color="#6380FF"/><stop offset="1" stop-color="#A386FF"/></linearGradient></defs>
<rect width="1080" height="1350" fill="#071521"/>
<path d="M-180 790L140 1110M955 -80L1190 155" stroke="#163344" stroke-width="140"/>
<path d="M0 1260L1080 1070V1350H0Z" fill="#102939"/>
{img(ROOT/'brand/v2/aurix-logo-horizontal-reverse-v2.svg',64,36,510,128)}
<path d="{ribbon}" stroke="#142D42" stroke-width="106" fill="none" transform="translate(0 15)"/>
<path d="{ribbon}" stroke="url(#ribbon)" stroke-width="[WIDTH]" fill="none" stroke-linecap="round"/>
<path d="M127 247V287M107 267H147M917 600V632M901 616H933" stroke="#FFD36A" stroke-width="7" stroke-linecap="round"/>
{img(ROOT/'brand/outline/official/outline-client-icon-1024.png',433,313,214,214)}
{text(639,'Outline VPN',53,family='Inter',group='category')}
{text(794,'UNLIMITED',[105,112,112][r],'#B5F9F1',family='Inter',group='offer')}
{text(948,'၇ ရက် အဝသုံး',[80,80,88][r],family='Noto Serif Myanmar',group='duration')}
<g transform="rotate(-3 540 1141)">
<path d="M100 1073H982L966 1209H84Z" fill="#FFD36A"/>
{text(1162,'အခမဲ့ Key ၅ ခု • ဦးသူယူ',[43,46,46][r],'#102534',group='scarcity')}
</g>
</svg>'''.replace('[WIDTH]',str([65,77,77][r]))
 source=out/(stem+'.svg');source.write_text(svg)
 for suf,w,h in [('',1080,1350),('_preview',324,405)]:
  subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(out/(stem+suf+'.png')),str(source)],check=True)
 if r==2:
  previous=ROOT/'assets/social/free-key-drop-v3/exports/20260907-041302_aurix-free-key-drop-v3_r2.txt'
  (out/(stem+'.txt')).write_text(previous.read_text())
if __name__=='__main__':render(int(sys.argv[1]) if len(sys.argv)>1 else 2)
