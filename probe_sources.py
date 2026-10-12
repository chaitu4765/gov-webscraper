import json, re, urllib.request, urllib.parse, http.cookiejar
from html import unescape
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

SOURCES = {
    'cppp': 'https://eprocure.gov.in/eprocure/app',
    'defproc': 'https://defproc.gov.in/nicgep/app',
    'hsl': 'https://eprocurehsl.nic.in/nicgep/app',
    'gem': 'https://bidplus.gem.gov.in/all-bids',
    'epublish': 'https://eprocure.gov.in/epublish/app',
}

def clean(s):
    return re.sub(r'\s+', ' ', unescape(re.sub('<[^>]+>', ' ', s))).strip()

def probe(item):
    name, url = item
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        with opener.open(urllib.request.Request(url, headers={'User-Agent': 'TenderResearch/0.1'}), timeout=35) as r:
            body = r.read().decode('utf-8', errors='replace')
            final = r.url
        Path('probe-results').mkdir(exist_ok=True)
        Path(f'probe-results/{name}.html').write_text(body, encoding='utf-8')
        records = []
        for row in re.findall(r'<tr\b[^>]*>.*?</tr>', body, re.I | re.S):
            if re.search(r'\d{2}-[A-Za-z]{3}-\d{4}', row):
                links = [{'title': clean(t), 'url': urllib.parse.urljoin(final, unescape(h))} for h,t in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', row, re.I | re.S)]
                if links and re.match(r'^\d+\.', links[0]['title']) and len(clean(row)) < 3000:
                    records.append({'text': clean(row), 'links': links})
        result = {'source':name,'requested':url,'final':final,'bytes':len(body),'records':records}
        Path(f'probe-results/{name}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'source': name, 'records': len(records), 'sample': records[:1]}), flush=True)
        if name == 'gem':
            token = re.search(r"'csrf_bd_gem_nk':\s*'([^']+)'", body).group(1)
            for page in [1, 2]:
                payload = {'page':page,'param':{'searchBid':'','searchType':'fullText'},'filter':{'bidStatusType':'ongoing_bids','byType':'all','highBidValue':'','byEndDate':{'from':'','to':''},'sort':'Bid-End-Date-Oldest'}}
                data = urllib.parse.urlencode({'payload':json.dumps(payload),'csrf_bd_gem_nk':token}).encode()
                req = urllib.request.Request('https://bidplus.gem.gov.in/all-bids-data', data=data, headers={'Referer':url,'X-Requested-With':'XMLHttpRequest','User-Agent':'TenderResearch/0.1'})
                with opener.open(req, timeout=35) as r:
                    response = json.load(r)
                Path(f'probe-results/gem-page-{page}.json').write_text(json.dumps(response, indent=2), encoding='utf-8')
                found = response.get('response',{}).get('response',{})
                print(json.dumps({'source':'gem','page':page,'code':response.get('code'),'total':found.get('numFound'),'count':len(found.get('docs',[])),'sample':found.get('docs',[])[:1]}), flush=True)
        elif records:
            for kind, record in [('tender',records[0]),('corrigendum',next((x for x in records if '%24DirectLink_0' in x['links'][0]['url']), records[-1]))]:
                detail_url = record['links'][0]['url']
                with opener.open(detail_url, timeout=35) as r:
                    detail = r.read().decode('utf-8',errors='replace')
                Path(f'probe-results/{name}-{kind}.html').write_text(detail,encoding='utf-8')
                text = clean(detail)
                pos = text.find('Tender ID')
                print(json.dumps({'source':name,'kind':kind,'detail_url':detail_url,'has_tender_id':pos>=0,'sample':text[max(0,pos-120):max(0,pos-120)+1400]}),flush=True)
    except Exception as e:
        print(json.dumps({'source':name,'error':str(e)}), flush=True)

if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(probe, SOURCES.items()))
