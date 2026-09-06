"""V2: single prize block, brand-led invitation, exact Burmese typography."""
from pathlib import Path
import base64, json, subprocess, sys
from xml.sax.saxutils import escape

ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parent
STAMP='20260907-035711'
def image(path,x,y,w,h):
    mime='image/svg+xml' if path.suffix=='.svg' else 'image/png'
    return f'<image href="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}" x="{x}" y="{y}" width="{w}" height="{h}"/>'
def text(y,s,size=40,color='#102E42',weight=600,family='Noto Sans Myanmar',group='flow'):
    return f'<text x="540" y="{y}" text-anchor="middle" xml:lang="my" font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{color}" data-mm-role="headline" data-mm-group="{group}" style="font-kerning:normal;font-feature-settings:\'mark\' 1,\'mkmk\' 1">{escape(s)}</text>'
def render(r):
    out=BASE/('exports' if r==2 else 'iterations');out.mkdir(exist_ok=True)
    stem=f'{STAMP}_aurix-unlimited-week-v2_r{r}'
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350">
<defs><linearGradient id="paper" x2="1" y2="1"><stop stop-color="#E7FAF6"/><stop offset="1" stop-color="#D4EAF7"/></linearGradient><linearGradient id="signal"><stop stop-color="#31D9F5"/><stop offset="1" stop-color="#7862F2"/></linearGradient></defs>
<rect width="1080" height="1350" fill="url(#paper)"/>
<path d="M-100 1110L200 1400M965 -70L1180 145" stroke="#FFFFFF" stroke-width="80" opacity=".45"/>
{image(ROOT/'brand/v2/aurix-logo-horizontal-v2.svg',70,45,[470,530,530][r],133)}
<text x="1000" y="114" text-anchor="end" font-family="Inter" font-size="25" font-weight="700" fill="#315366">GIVEAWAY</text>
{text(279,'AuriX နဲ့ စမ်းသုံးကြည့်မလား?',57,weight=700)}
<g transform="rotate(-1.3 540 683)">
<rect x="107" y="384" width="878" height="618" rx="9" fill="#123748" opacity=".12"/>
<rect x="94" y="365" width="878" height="618" rx="9" fill="#FFFDF5"/>
<rect x="94" y="365" width="878" height="7" fill="url(#signal)"/>
{image(ROOT/'brand/outline/official/outline-client-icon-1024.png',222,408,104,104)}
<text x="361" y="480" font-family="Inter" font-size="61" font-weight="700" fill="#102E42">Outline VPN</text>
{text(651,'၇ ရက် အဝသုံး',[88,88,94][r],weight=800,family='Noto Serif Myanmar',group='prize')}
{('<path d="M346 689Q540 706 734 689" stroke="#FFC857" stroke-width="12" fill="none" stroke-linecap="round"/>') if r==0 else ''}
{text(765,'VPN Quota မကန့်သတ်ပါ',38,weight=500,group='quota')}
<path d="M94 813H972V974Q972 983 963 983H103Q94 983 94 974Z" fill="#102E42"/>
<path d="M122 813H944" stroke="#AEC6CB" stroke-width="2" stroke-dasharray="9 10"/>
<circle cx="94" cy="813" r="22" fill="#DFF3F6"/><circle cx="972" cy="813" r="22" fill="#DFF3F6"/>
{text(882,'ကံထူးရှင် ၅ ယောက်',49,'#FFFDF5',700,group='winners')}
{text(956,'တစ်ယောက်ကို Key တစ်ခုစီ အခမဲ့',35,'#DDF4F4',500,group='winners')}
</g>
{text(1100,'“စမ်းသုံးမယ်” လို့',49,weight=800,group='cta')}
{text(1178,'Comment ရေးခဲ့ပါ',42,weight=600,group='cta')}
{text(1270,'ဝယ်စရာမလိုပါ • အသေးစိတ် Caption မှာ ဖတ်ပေးနော်',24,'#3A5E6D',500,group='footer')}
</svg>'''
    source=out/(stem+'.svg');source.write_text(svg)
    for suffix,w,h in [('',1080,1350),('_preview',324,405)]:
        subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(out/(stem+suffix+'.png')),str(source)],check=True)
if __name__=='__main__':render(int(sys.argv[1]) if len(sys.argv)>1 else 2)
