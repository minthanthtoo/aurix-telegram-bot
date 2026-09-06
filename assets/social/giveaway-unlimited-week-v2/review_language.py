"""Audit exact final text through verified Flash 3.8, without model fallback."""
import json, pathlib, urllib.request, xml.etree.ElementTree as ET
ROOT=pathlib.Path(__file__).resolve().parents[3]
BASE=pathlib.Path(__file__).resolve().parent
env={}
for line in (ROOT/'.env').read_text().splitlines():
    key,sep,value=line.partition('=')
    if sep and key.strip() in ('RECEIPT_LLM_BASE_URL','RECEIPT_LLM_API_KEY'):
        env[key.strip()]=value.strip().strip('\"\'')
stem=BASE/'exports/20260907-035711_aurix-unlimited-week-v2_r2'
lines=[''.join(t.itertext()) for t in ET.parse(stem.with_suffix('.svg')).getroot().iter('{http://www.w3.org/2000/svg}text')]
prompt='''Audit exact Burmese language/content, not pixels, as a Myanmar advertising editor. This is V2. Image has approved AuriX logo and official Outline icon. Friendly spoken register, not formal translationese. Facts locked: FIVE winners, ONE Outline VPN key each, unlimited VPN quota for SEVEN days, not free mobile data or guaranteed speed. Entry proposed, pinned terms must be filled before publishing; don't invent dates. No Meta algorithm claims. Give verdict, max3 issues with precise replacements, score/10. Under 300 words. Image text:\n'''+ '\n'.join(lines)+'\nCaption:\n'+stem.with_suffix('.txt').read_text()
model='ag/gemini-3.8-flash-tiered(high)'
req=urllib.request.Request(env['RECEIPT_LLM_BASE_URL'].rstrip('/')+'/chat/completions',data=json.dumps({'model':model,'messages':[{'role':'user','content':prompt}],'stream':False}).encode(),headers={'Authorization':'Bearer '+env['RECEIPT_LLM_API_KEY'],'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=55) as response: result=json.load(response)
    if result.get('model')!='gemini-3.8-flash-tiered': raise ValueError('Unexpected returned model')
    safe={'requested_model':model,'returned_model':result['model'],'review':result['choices'][0]['message']['content']}
    (BASE/'language-review-gemini38.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2))
    print(json.dumps(safe,ensure_ascii=False))
except Exception as e:
    print('Review failed:',type(e).__name__)
    raise SystemExit(1)
