"""Compose existing mascot with exact Burmese typography and official marks."""
from pathlib import Path
from xml.sax.saxutils import escape
import base64,json,subprocess,xml.etree.ElementTree as ET
BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[2]
STAMP='20260916-170000'
OUT=BASE/'exports'; OUT.mkdir(exist_ok=True)
compact=ET.parse(ROOT/'brand/v2/aurix-logo-horizontal-v2.svg')
for node in list(compact.getroot()):
    if node.tag.endswith('text') and 'Clear access.' in ''.join(node.itertext()):compact.getroot().remove(node)
compact.getroot().set('viewBox','0 0 900 320')
compact.write(BASE/'aurix-compact-derived.svg',encoding='utf-8',xml_declaration=True)
NAVY='#071421'; CYAN='#36E2FF'; GOLD='#FFC857'
def image(path,x,y,w,h):
    path=ROOT/path
    mime='image/svg+xml' if path.suffix=='.svg' else 'image/jpeg' if path.suffix=='.jpg' else 'image/png'
    return f'<image x="{x}" y="{y}" width="{w}" height="{h}" href="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"/>'
def txt(s,x,y,size=48,color=NAVY,family='Noto Sans Myanmar',group='main',weight=700,anchor='start'):
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" fill="{color}" xml:lang="my" data-mm-role="headline" data-mm-group="{group}" style="font-kerning:normal;font-feature-settings:\'mark\' 1,\'mkmk\' 1">{escape(s)}</text>'
def mascot(x,y,w,h): return image(Path('brand/mascot-v2-3d/20260916_aurix-auri-3d-hero-r2.png'),x,y,w,h)
def outline(x,y,size=88):return image(Path('brand/outline/official/outline-client-icon-1024.png'),x,y,size,size)
def render(item,round=2):
    n=int(item['id'][:2]); lines=item['lines']; body=''
    # Round 1 improves reading order; round 2 uses safer Myanmar baselines.
    gap=[78,88,102][round]
    body+=image(Path('assets/social/mascot-samples-v1/aurix-compact-derived.svg'),64,24,390,139)
    if n!=2:
        body+=outline(888,58,78)
        body+=txt('Outline VPN',927,169,22,group='category',family='Inter',anchor='middle')
    if n==1:
        body+=txt(lines[0],72,273,66,family='Noto Serif Myanmar')+txt(lines[1],72,273+gap,53)
        body+='<path d="M76 412H322" stroke="#36E2FF" stroke-width="13"/>'
        body+=mascot(-15,468,650,812)
        body+='<circle cx="795" cy="661" r="129" fill="none" stroke="#E8EEF3" stroke-width="24"/><path d="M795 532 A129 129 0 1 1 666 661" fill="none" stroke="#7765FF" stroke-width="24" stroke-linecap="round"/>'
        body+=txt('Quota',795,650,45,group='motif',family='Inter',anchor='middle')+txt('လက်ကျန်',795,724,40,group='motif',anchor='middle')
        body+=txt('AuriX မှာ ဝယ်ထားတဲ့',619,898,32,group='proof')+txt('Key တွေအတွက်',619,956,36,group='proof')
        body+=txt('နည်းလာရင်လည်း',619,1052,34,group='reminder')+txt('သတိပေးပါတယ်',619,1112,34,group='reminder')
    elif n==2:
        body+=txt(lines[0],72,268,55)+txt(lines[1],72,268+gap,60,family='Noto Serif Myanmar')
        body+='<path d="M76 410H362" stroke="#FFC857" stroke-width="13"/>'
        body+=mascot(-40,465,650,812)
        body+='<path d="M685 494H899V740L881 726L863 740L845 726L827 740L809 726L791 740L773 726L755 740L737 726L719 740L701 726L685 740Z" fill="#FFF3D5" stroke="#D8B564" stroke-width="4"/><path d="M727 549H854M727 581H828" stroke="#C6AA6B" stroke-width="9" stroke-linecap="round"/><circle cx="794" cy="661" r="39" fill="#071421"/><path d="M775 660L789 674L814 645" stroke="#36E2FF" stroke-width="8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        body+=txt('AI နဲ့ စစ်ပေးမယ်',797,817,37,group='process',anchor='middle')
        body+='<path d="M794 853V898M780 884L794 898L808 884" fill="none" stroke="#7765FF" stroke-width="6"/>'
        body+=outline(752,930,86)
        body+=txt('အတည်ပြုပြီးတာနဲ့',797,1082,33,group='result',anchor='middle')+txt('Key ရမယ်',797,1142,40,group='result',anchor='middle')
    elif n==3:
        body+=txt(lines[0],72,276,82,family='Noto Serif Myanmar')+txt(lines[1],72,276+gap+24,53)
        body+=mascot(386,450,660,825)
        body+='<path d="M91 522H368Q399 522 399 553V733Q399 764 368 764H190L123 815V764H91Q60 764 60 733V553Q60 522 91 522Z" fill="#EDF1FF"/>'
        body+=txt('?',230,707,150,'#7765FF',family='Inter',group='question',anchor='middle')
        body+=txt('တစ်ယောက်တည်း',76,941,41,group='help')+txt('စမ်းနေစရာမလိုပါဘူး',76,1019,38,group='help')
        body+=txt('AuriX Group မှာ မေးလိုက်ပါ',72,1155,39,group='action')
    else:
        body+=txt(lines[0],72,275,61,family='Noto Serif Myanmar')
        body+=txt('300',64,551,250,NAVY,family='Inter',group='offer')+txt('MB',615,550,106,'#7765FF',family='Inter',group='unit')
        body+='<path d="M77 599H1002" stroke="#FFC857" stroke-width="16"/>'
        body+=mascot(465,607,510,637)
        body+=txt('နေ့စဉ်',73,798,67,group='free')+txt('အခမဲ့',73,940,83,group='free',family='Noto Serif Myanmar')
        body+=txt('Outline VPN',74,1041,41,group='freecategory',family='Inter')+txt('Bot မှာ ရယူပါ',74,1120,41,group='freecategory')
    body+='<rect y="1240" width="1080" height="110" fill="#071421"/>'
    dest='AuriX Telegram Group • Link in caption' if n==3 else '@aurix_outline_vpn_bot'
    body+=txt(dest,540,1306,29 if n==3 else 34,'#F6F8FC',family='Inter',group='destination',anchor='middle')
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1350" viewBox="0 0 1080 1350"><rect width="1080" height="1350" fill="white"/>'+body+'</svg>'
    stem=f'{STAMP}_aurix-{item["id"]}_r{round}'
    folder=OUT if round==2 else BASE/'iterations';folder.mkdir(exist_ok=True)
    source=folder/(stem+'.svg');source.write_text(svg)
    for suffix,w,h in [('',1080,1350),('_preview',324,405)]:
        subprocess.run(['rsvg-convert','-w',str(w),'-h',str(h),'-o',str(folder/(stem+suffix+'.png')),str(source)],check=True)
    caption=item['caption'].replace('စစ်ဆေးလို့မရတဲ့ အော်ဒါတွေကိုတော့','အတည်မပြုနိုင်တဲ့ အော်ဒါတွေကိုတော့').replace('အကူအညီမေးရန်','အကူအညီရယူရန်')
    (folder/(stem+'.txt')).write_text(caption+'\n')
    return {'id':item['id'],'role':item['role'],'image':str((folder/(stem+'.png')).relative_to(BASE)),'caption':str((folder/(stem+'.txt')).relative_to(BASE)),'source':str(source.relative_to(BASE))}
if __name__=='__main__':
    import sys
    r=int(sys.argv[1]) if len(sys.argv)>1 else 2
    items=json.loads((BASE/'content.json').read_text())
    finals=[render(item,r) for item in items]
    if r==2:(BASE/'manifest.json').write_text(json.dumps({'campaign':'auri-mascot-samples-v1','stamp':STAMP,'format':'1080x1350 PNG + UTF-8 TXT + SVG','recommended_order':[i['id'] for i in items],'finals':finals,'publication_gates':['Sample creative: confirm live daily free eligibility and automatic receipt flow before publishing.','No exact reminder thresholds advertised; older sources conflict.','Gemini audit availability recorded separately.']},indent=2))
