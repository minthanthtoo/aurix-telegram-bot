"""First-come key drop, not a lucky draw. Authored vector poster."""
from pathlib import Path
import base64, subprocess, sys
from xml.sax.saxutils import escape
ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parent
STAMP='20260907-041302'
def img(path,x,y,w,h):
    mime='image/svg+xml' if path.suffix=='.svg' else 'image/png'
    return f'<image href="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}" x="{x}" y="{y}" width="{w}" height="{h}"/>'
def t(y,s,size=40,fill='#102E42',weight=600,group='copy',family='Noto Sans Myanmar'):
    return f'<text x="540" y="{y}" text-anchor="middle" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{fill}" xml:lang="my" data-mm-role="headline" data-mm-group="{group}" style="font-kerning:normal;font-feature-settings:\'mark\' 1,\'mkmk\' 1">{escape(s)}</text>'
def render(r):
    out=BASE/('exports' if r==2 else 'iterations');out.mkdir(exist_ok=True)
    stem=f'{STAMP}_aurix-free-key-drop-v3_r{r}'
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#E6FAF5"/><stop offset="1" stop-color="#D8EAF9"/></linearGradient><linearGradient id="signal"><stop stop-color="#31D9F5"/><stop offset="1" stop-color="#7862F2"/></linearGradient></defs>
<rect width="1080" height="1350" fill="url(#bg)"/>
<path d="M938 -40L1120 160M-80 720L100 890" stroke="#FFF" stroke-width="60" opacity=".4"/>
{img(ROOT/'brand/v2/aurix-logo-horizontal-v2.svg',70,38,490,123)}
{img(ROOT/'brand/outline/official/outline-client-icon-1024.png',336,191,72,72)}
<text x="434" y="243" font-family="Inter" font-size="42" font-weight="700" fill="#102E42">Outline VPN</text>
{t(375,'အခမဲ့ Key ၅ ခု',[83,86,86][r],weight=800,family='Noto Serif Myanmar',group='hero')}
{t(510,'Comment ထဲမှာ ယူလိုက်ပါ',54,weight=700,group='hero')}
<g transform="rotate(-1.5 540 749)">
<rect x="101" y="624" width="894" height="271" rx="8" fill="#123748" opacity=".10"/>
<path d="M88 606H980V858H88V751A20 20 0 0 0 88 711Z" fill="#FFFDF5"/>
<rect x="88" y="606" width="892" height="7" fill="url(#signal)"/>
{t(711,'၇ ရက် အဝသုံး',72,weight=800,family='Noto Serif Myanmar',group='offer')}
{t(824,'VPN Quota မကန့်သတ်ပါ',37,weight=500,group='offer')}
</g>
<path d="M0 937Q540 900 1080 937V1350H0Z" fill="#102E42"/>
{t(1013,'ဦးသူယူပါ • တစ်ယောက်တစ်ခု',42,'#FFD36A',700,group='claim')}
{t(1091,'ယူပြီးရင် “ယူပြီးပါပြီ” လို့ Reply ပေးခဲ့ပါ',32,'#F4FAF8',500,group='claim') if r<2 else ''}
<path d="M240 1090H840" stroke="#547A8A" stroke-width="1"/>
{t(1180,'Free Key ထပ်လိုချင်ရင်',39,'#D9F3F2',500,group='group')}
{t(1250,'Telegram Group မှာ မေးလို့ရပါတယ်',39,'#F4FAF8',600,group='group')}
</svg>'''
    source=out/(stem+'.svg');source.write_text(svg)
    for suff,w,h in [('',1080,1350),('_preview',324,405)]:
        subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(out/(stem+suff+'.png')),str(source)],check=True)
if __name__=='__main__':render(int(sys.argv[1]) if len(sys.argv)>1 else 2)
