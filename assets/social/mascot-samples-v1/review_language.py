"""Bounded per-asset exact-copy council; never prints credentials."""
import concurrent.futures, json, pathlib, urllib.request, xml.etree.ElementTree as ET, sys
BASE=pathlib.Path(__file__).resolve().parent
ROOT=BASE.parents[2]
env={}
for line in (ROOT/'.env').read_text().splitlines():
    key,sep,value=line.partition('=')
    if sep and key.strip() in ('RECEIPT_LLM_BASE_URL','RECEIPT_LLM_API_KEY'):
        env[key.strip()]=value.strip().strip('\"\'')
model='ag/gemini-3.8-flash-tiered(high)'
def review(item):
    if '--final' in sys.argv:
        stem=BASE/'exports'/('20260916-170000_aurix-'+item['id']+'_r2')
        item=dict(item)
        item['lines']=[''.join(node.itertext()) for node in ET.parse(stem.with_suffix('.svg')).getroot().iter('{http://www.w3.org/2000/svg}text')]
        item['caption']=stem.with_suffix('.txt').read_text()
    prompt='''Audit this ONE Burmese advertising sample as a native Myanmar copy editor. Return verdict, max 3 precise language issues/replacements, score. Do not invent facts or rewrite strategy. Friendly adult consumer register. Locked facts: Outline VPN sold via AuriX Telegram bot; quota lookup for keys bought there; low-quota reminders (no fixed percentages); AI receipt check, key issued only after successful verification, no guaranteed duration; 300 MB daily free is VPN allowance not mobile data. Support group is not guaranteed instant/24h. Preserve URLs and wallet names. No need to add price or details to the image. Exact image lines and caption follow:\n'''+json.dumps(item,ensure_ascii=False)
    req=urllib.request.Request(env['RECEIPT_LLM_BASE_URL'].rstrip('/')+'/chat/completions',data=json.dumps({'model':model,'messages':[{'role':'user','content':prompt}],'stream':False}).encode(),headers={'Authorization':'Bearer '+env['RECEIPT_LLM_API_KEY'],'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=50) as response: result=json.load(response)
        returned=result.get('model')
        if returned!='gemini-3.8-flash-tiered': raise ValueError('model mismatch')
        safe={'id':item['id'],'requested':model,'returned':returned,'review':result['choices'][0]['message']['content']}
    except Exception as e: safe={'id':item['id'],'status':'unavailable','error_type':type(e).__name__}
    (BASE/(item['id']+('-final' if '--final' in sys.argv else '')+'-language-review.json')).write_text(json.dumps(safe,ensure_ascii=False,indent=2))
    return safe
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for result in pool.map(review,json.loads((BASE/'content.json').read_text())):
        print(json.dumps(result,ensure_ascii=False))
