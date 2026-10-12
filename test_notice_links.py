import json
from urllib.request import urlopen
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from probe_sources import clean
from app import public_notice_url

def check(feed):
    data=json.loads(Path(f'probe-results/{feed}.json').read_text())
    rows=[next(r for r in data['records'] if marker in r['links'][0]['url']) for marker in ['%24DirectLink&','%24DirectLink_0&']]
    for row in rows:
        mode='corrigendum' if '%24DirectLink_0' in row['links'][0]['url'] else 'tender'
        url=public_notice_url(row['links'][0]['url'])
        try:
            with urlopen(url,timeout=25) as response:
                text=clean(response.read().decode('utf-8',errors='replace'))
            pos=text.find('Tender ID')
            print(json.dumps({'feed':feed,'mode':mode,'tender':pos>=0,'stale':'session has timed out' in text,'sample':text[max(0,pos-70):max(0,pos-70)+200]}),flush=True)
        except Exception as e: print(feed,mode,str(e),flush=True)

with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(check,['cppp','epublish','defproc','hsl']))
