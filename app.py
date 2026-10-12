"""TenderDesk: local dashboard, SQLite archive, and scheduled public collectors."""
import csv
import auth
import hashlib
import http.cookiejar
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler
from html.parser import HTMLParser
from html import unescape

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('TENDER_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'tenders.sqlite3'
IST = timezone(timedelta(hours=5, minutes=30))
PORT = int(os.environ.get('PORT', '8765'))
FEEDS = {
    'gem': ('GeM', 'https://bidplus.gem.gov.in/all-bids', 'Paginated public bids; recent pages plus a rolling backfill.'),
    'cppp': ('CPPP · eProcurement', 'https://eprocure.gov.in/eprocure/app', 'Latest 10 tenders and 10 corrigenda; not a complete active-tender inventory.'),
    'epublish': ('CPPP · ePublishing', 'https://eprocure.gov.in/epublish/app', 'Latest 10 tenders and 10 corrigenda; separate from eProcurement.'),
    'defproc': ('Defence procurement', 'https://defproc.gov.in/nicgep/app', 'Latest homepage tenders and corrigenda; not exhaustive.'),
    'hsl': ('Hindustan Shipyard', 'https://eprocurehsl.nic.in/nicgep/app', 'Latest homepage tenders and corrigenda from the working NIC portal.'),
}
SYNC_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
STATE = {'running': False, 'current': [], 'next_run': None, 'last_finished': None}
STOP = threading.Event()
WAKE = threading.Event()
DOCUMENT_LOCK = threading.Lock()
RETRY_AFTER = {}
FAILURES = {}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def connect():
    conn = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS notices (
          id TEXT PRIMARY KEY, feed TEXT NOT NULL, kind TEXT NOT NULL,
          title TEXT NOT NULL, reference TEXT NOT NULL, organisation TEXT,
          category TEXT, deadline TEXT, opening TEXT, url TEXT NOT NULL,
          description TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          updated_at TEXT NOT NULL, fingerprint TEXT NOT NULL, bookmarked INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS notices_feed ON notices(feed);
        CREATE INDEX IF NOT EXISTS notices_deadline ON notices(deadline);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES notices(id),
          event_type TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_notice ON events(notice_id);
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, label TEXT, url TEXT, coverage TEXT, state TEXT DEFAULT 'pending',
          last_attempt TEXT, last_success TEXT, error TEXT, fetched INTEGER DEFAULT 0,
          new_count INTEGER DEFAULT 0, changed_count INTEGER DEFAULT 0,
          duration REAL, remote_total INTEGER, cursor INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, feed TEXT, started TEXT, finished TEXT,
          state TEXT, fetched INTEGER DEFAULT 0, new_count INTEGER DEFAULT 0,
          changed_count INTEGER DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, notice_id TEXT NOT NULL REFERENCES notices(id),
          name TEXT NOT NULL, source_url TEXT NOT NULL, fetched TEXT NOT NULL, body BLOB NOT NULL);
        ''')
        for feed, (label, url, coverage) in FEEDS.items():
            c.execute('INSERT OR IGNORE INTO sources(id,label,url,coverage) VALUES(?,?,?,?)', (feed,label,url,coverage))
        for key,value in [('interval_seconds','30'),('gem_pages','3'),('gem_backfill_pages','2')]:
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key,value))
        c.execute("DELETE FROM settings WHERE key='interval_minutes'")
        auth.initialize(c)
        # Homepage direct links include the collector's required-session flag.
        # These public links carry their own notice identifier and work with a new
        # portal session when that flag is omitted. Repair existing records too.
        for row in c.execute('SELECT id,url FROM notices').fetchall():
            fixed=public_notice_url(row['url'])
            if fixed!=row['url']:
                c.execute('UPDATE notices SET url=? WHERE id=?',(fixed,row['id']))
        for row in c.execute('SELECT id,payload FROM events').fetchall():
            payload=json.loads(row['payload'])
            fixed=public_notice_url(payload.get('url',''))
            if fixed!=payload.get('url',''):
                payload['url']=fixed
                c.execute('UPDATE events SET payload=? WHERE id=?',(json.dumps(payload),row['id']))
        # A terminated collector must never look as if it is still working.
        c.execute("UPDATE sources SET state='interrupted',error='Collector stopped before completion; a new attempt will run.' WHERE state='running'")

def settings():
    with connect() as c:
        return {r['key']: int(r['value']) for r in c.execute('SELECT * FROM settings')}

def clean(s):
    return re.sub(r'\s+', ' ', unescape(re.sub('<[^>]+>', ' ', s))).strip()

def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()

def date_value(s, gem=False):
    if not s:
        return None
    try:
        if gem:
            # GeM's public UI formats these strings using UTC getters, displaying the
            # literal wall clock. Interpret that displayed clock in India's timezone.
            dt = datetime.fromisoformat(s.replace('Z','+00:00')).replace(tzinfo=IST)
        else:
            dt = datetime.strptime(s, '%d-%b-%Y %I:%M %p').replace(tzinfo=IST)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except ValueError:
        return None

def allowed_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None,443) and parsed.hostname in {
        'bidplus.gem.gov.in','eprocure.gov.in','defproc.gov.in','eprocurehsl.nic.in'}

def public_notice_url(url):
    """Normalize only verified public homepage notice links, not session forms."""
    parsed=urlparse(url)
    pairs=parse_qsl(parsed.query,keep_blank_values=True)
    query=dict(pairs)
    if (parsed.scheme=='https' and parsed.hostname in
        {'eprocure.gov.in','defproc.gov.in','eprocurehsl.nic.in'}
        and query.get('page')=='Home' and query.get('service')=='direct'
        and query.get('component') in ('$DirectLink','$DirectLink_0')
        and query.get('sp') and 'session' in query):
        return urlunparse(parsed._replace(query=urlencode([(k,v) for k,v in pairs if k!='session'])))
    return url

def parse_gep(body, feed, base):
    records = []
    for row in re.findall(r'<tr\b[^>]*>.*?</tr>', body, re.I | re.S):
        links = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', row, re.I | re.S)
        if not links or not re.match(r'^\d+\.', clean(links[0][1])):
            continue
        text = clean(row)
        dates = re.findall(r'\d{2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}\s+[AP]M', text)
        if len(dates) < 2 or len(text)>3000:
            continue
        url = public_notice_url(urljoin(base,unescape(links[0][0])))
        if not allowed_url(url):
            continue
        original_title = clean(links[0][1])
        title = re.sub(r'^\d+\.\s*', '', original_title)
        reference = text[len(original_title):text.find(dates[-2])].strip()
        if not reference:
            reference = title
        kind = 'corrigendum' if '$DirectLink_0' in unescape(url).replace('%24','$') else 'tender'
        # Corrigenda with distinct titles are separate notices; edits to the same
        # reference/title become revisions. Ambiguous same-title notices remain a
        # known coverage limitation of the homepage feed.
        key = f'{feed}|{kind}|{reference}' + (f'|{title}' if kind=='corrigendum' else '')
        records.append(dict(id=digest(key)[:24],feed=feed,kind=kind,title=title,
            reference=reference,organisation='Hindustan Shipyard Limited' if feed=='hsl' else '',
            category='',deadline=date_value(dates[-2]),opening=date_value(dates[-1]),
            url=url,description=re.sub(r'^\d+\.\s*','',text)))
    return records

def scalar(d,key,default=''):
    value=d.get(key,default)
    return value[0] if isinstance(value,list) and value else (default if isinstance(value,list) else value)

def parse_gem(response):
    if response.get('code') != 200:
        raise ValueError('GeM did not return a successful bid-list response')
    result=response.get('response',{}).get('response',{})
    records=[]
    for d in result.get('docs',[]):
        bid=str(scalar(d,'b_id',d.get('id','')))
        ref=scalar(d,'b_bid_number')
        if not bid or not ref:
            continue
        typ=scalar(d,'b_bid_type',0)
        path='showdirectradocumentPdf' if typ==5 else ('showradocumentPdf' if typ==2 else 'showbidDocument')
        if typ==2 and scalar(d,'b_eval_type',0)>0:
            path='list-ra-schedules'
        title=scalar(d,'bd_category_name') or scalar(d,'b_category_name') or ref
        records.append(dict(id=digest('gem|'+bid)[:24],feed='gem',kind='auction' if typ in (2,5) else 'tender',
            title=title,reference=ref,organisation=' · '.join(filter(None,[scalar(d,'ba_official_details_minName'),scalar(d,'ba_official_details_deptName')])),
            category=scalar(d,'b_category_name'),deadline=date_value(scalar(d,'final_end_date_sort'),True),
            opening=None,url=f'https://bidplus.gem.gov.in/{path}/{bid}',description=f'Quantity: {scalar(d,"b_total_quantity","Not specified")}. Public GeM bid listing.'))
    return records, result.get('numFound',0)

def save_records(feed, records, at=None, event_type='new'):
    at=at or now()
    added=changed=0
    with connect() as c:
        for r in records:
            r={**r,'url':public_notice_url(r['url'])}
            # Session link churn is not a procurement amendment.
            def semantic(item):
                fields={k:item[k] for k in r if k not in ('url','id')}
                if feed!='gem':
                    fields['description']=re.sub(r'^\d+\.\s*','',fields['description'])
                return digest(json.dumps(fields,sort_keys=True))
            fingerprint=semantic(r)
            old=c.execute('SELECT * FROM notices WHERE id=?',(r['id'],)).fetchone()
            if not old:
                cols=list(r)+['first_seen','last_seen','updated_at','fingerprint']
                c.execute(f'INSERT INTO notices({",".join(cols)}) VALUES({",".join("?" for _ in cols)})',list(r.values())+[at,at,at,fingerprint])
                added+=1
            elif semantic(old)!=fingerprint:
                c.execute('UPDATE notices SET '+','.join(k+'=?' for k in r if k!='id')+',last_seen=?,updated_at=?,fingerprint=? WHERE id=?',
                    [v for k,v in r.items() if k!='id']+[at,at,fingerprint,r['id']])
                changed+=1
            else:
                c.execute('UPDATE notices SET last_seen=?,url=? WHERE id=?',(at,r['url'],r['id']))
                continue
            c.execute('INSERT INTO events(notice_id,event_type,at,payload) VALUES(?,?,?,?)',
                (r['id'],'updated' if old else event_type,at,json.dumps(r)))
    return added,changed

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError('Document redirect outside supported official portals')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class DocumentLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.current=None
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='a':
            self.current=[attrs.get('href',''),'']
        elif tag in ('iframe','embed'):
            self.links.append((attrs.get('src',''),'PDF document'))
        elif tag=='object':
            self.links.append((attrs.get('data',''),'PDF document'))
    def handle_data(self, value):
        if self.current is not None:
            self.current[1]+=value
    def handle_endtag(self, tag):
        if tag=='a' and self.current is not None:
            self.links.append(tuple(self.current)); self.current=None

def document_candidates(body, base):
    parser=DocumentLinks(); parser.feed(body)
    found={}
    for href,label in parser.links:
        url=urljoin(base,href)
        if 'StandardBiddingDocuments' in url or 'zip' in label.lower():
            continue
        if allowed_url(url) and re.search(r'\.pdf(?:\b|$)|download|showbidDocument|showradocumentPdf|showdirectradocumentPdf',url+' '+label,re.I):
            found[url]=clean(label) or 'Tender document'
    return list(found.items())[:12]

def document_list(key):
    with connect() as c:
        return [dict(r) for r in c.execute('SELECT id,name,fetched FROM documents WHERE notice_id=? ORDER BY name',(key,))]

def fetch_documents(key):
    with connect() as c:
        notice=c.execute('SELECT * FROM notices WHERE id=?',(key,)).fetchone()
    if not notice:
        raise ValueError('Notice not found')
    cached=document_list(key)
    if cached:
        return {'documents':cached,'message':'Saved PDF copies. Check the official portal for later revisions.'}
    if not DOCUMENT_LOCK.acquire(blocking=False):
        raise ValueError('Another document request is running. Please retry shortly.')
    try:
        client=Client()
        body,base=client.binary(notice['url'])
        candidates=[(base,notice['reference'],body)] if body.startswith(b'%PDF-') else []
        if not candidates:
            for url,label in document_candidates(body.decode('utf-8',errors='replace'),base):
                try:
                    pdf,final_url=client.binary(url)
                    if pdf.startswith(b'%PDF-'):
                        candidates.append((final_url,label,pdf))
                except Exception:
                    logging.exception('Document unavailable for %s',key)
        with connect() as c:
            for url,label,pdf in candidates:
                name=re.sub(r'[^A-Za-z0-9._-]+','_',label)[:120].strip('._') or 'tender-document'
                if not name.lower().endswith('.pdf'):
                    name+='.pdf'
                doc_id=digest(key+digest(pdf.hex()))[:32]
                c.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?)',(doc_id,key,name,url,now(),pdf))
        docs=document_list(key)
        return {'documents':docs,'message':('PDFs saved locally. Downloads below come directly from TenderDesk.' if docs else 'No directly accessible PDF was returned. The portal may require CAPTCHA, login, or an interactive document selection. Use the official notice; these restrictions are not bypassed.')}
    finally:
        DOCUMENT_LOCK.release()

class Client:
    def __init__(self):
        self.opener=build_opener(SafeRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def binary(self,url):
        if not allowed_url(url):
            raise ValueError('Unexpected document URL')
        with self.opener.open(Request(url,headers={'User-Agent':'TenderDesk/1.0 public-notice-monitor'}),timeout=20) as r:
            body=r.read(25_000_001)
            if len(body)>25_000_000:
                raise ValueError('Document exceeds 25 MB download limit')
            return body,r.geturl()
    def get(self,url,data=None):
        if not allowed_url(url):
            raise ValueError('Unexpected source URL')
        headers={'User-Agent':'TenderDesk/1.0 public-notice-monitor','Referer':FEEDS['gem'][1] if 'gem.gov.in' in url else url}
        if data is not None:
            headers['X-Requested-With']='XMLHttpRequest'
        for attempt in range(2):
            try:
                with self.opener.open(Request(url,data=data,headers=headers),timeout=30) as r:
                    body=r.read(8_000_001)
                    if len(body)>8_000_000:
                        raise ValueError('Source response exceeds size limit')
                    return body.decode('utf-8',errors='replace')
            except HTTPError as e:
                if e.code not in (429,500,502,503,504) or attempt:
                    raise
                time.sleep(3)
            except (TimeoutError,OSError):
                if attempt:
                    raise
                time.sleep(2)

def collect(feed):
    if time.monotonic()<RETRY_AFTER.get(feed,0):
        return
    start=time.monotonic(); started=now()
    with STATE_LOCK:
        STATE['current'].append(feed)
    with connect() as c:
        c.execute("UPDATE sources SET state='running',last_attempt=?,error=NULL WHERE id=?",(started,feed))
        cursor=c.execute('SELECT cursor FROM sources WHERE id=?',(feed,)).fetchone()[0]
    added=changed=0; records=[]; total=None; new_cursor=cursor
    try:
        client=Client(); body=client.get(FEEDS[feed][1])
        if feed=='gem':
            match=re.search(r"'csrf_bd_gem_nk':\s*'([^']+)'",body)
            if not match:
                raise ValueError('GeM listing/token unavailable; possible blocked or changed page')
            cfg=settings(); first=cfg['gem_pages']; cursor=max(cursor,first+1)
            pages=list(range(1,first+1))+list(range(cursor,cursor+cfg['gem_backfill_pages']))
            for page in pages:
                payload={'page':page,'param':{'searchBid':'','searchType':'fullText'},'filter':{'bidStatusType':'ongoing_bids','byType':'all','highBidValue':'','byEndDate':{'from':'','to':''},'sort':'Bid-Start-Date-Latest'}}
                data=urlencode({'payload':json.dumps(payload),'csrf_bd_gem_nk':match.group(1)}).encode()
                rows,total=parse_gem(json.loads(client.get('https://bidplus.gem.gov.in/all-bids-data',data)))
                records.extend(rows)
                if page>=cursor:
                    new_cursor=page+1
                if page*10>=total:
                    new_cursor=first+1
                    break
                time.sleep(.6)
        else:
            records=parse_gep(body,feed,FEEDS[feed][1])
        if not records:
            raise ValueError('No notice records parsed; source may be empty, blocked or changed. Stored records retained.')
        records=list({r['id']:r for r in records}.values())
        added,changed=save_records(feed,records)
        finished=now()
        with connect() as c:
            c.execute("UPDATE sources SET state='ok',last_success=?,error=NULL,fetched=?,new_count=?,changed_count=?,duration=?,remote_total=?,cursor=? WHERE id=?",
                (finished,len(records),added,changed,round(time.monotonic()-start,2),total,new_cursor,feed))
            c.execute('INSERT INTO runs(feed,started,finished,state,fetched,new_count,changed_count) VALUES(?,?,?,?,?,?,?)',(feed,started,finished,'ok',len(records),added,changed))
        logging.info('%s: %s fetched, %s new, %s changed',feed,len(records),added,changed)
        FAILURES.pop(feed,None); RETRY_AFTER.pop(feed,None)
    except Exception as e:
        FAILURES[feed]=FAILURES.get(feed,0)+1
        RETRY_AFTER[feed]=time.monotonic()+min(1800,60*2**min(FAILURES[feed]-1,5))
        error=f'{type(e).__name__}: {e}'[:500]
        with connect() as c:
            c.execute("UPDATE sources SET state='error',error=?,duration=? WHERE id=?",(error,round(time.monotonic()-start,2),feed))
            c.execute('INSERT INTO runs(feed,started,finished,state,error) VALUES(?,?,?,?,?)',(feed,started,now(),'error',error))
        logging.warning('%s: %s',feed,error)
    finally:
        with STATE_LOCK:
            STATE['current'].remove(feed)

def sync_all():
    if not SYNC_LOCK.acquire(blocking=False):
        return False
    with STATE_LOCK:
        STATE['running']=True
    try:
        # Independent requests use at most two source connections concurrently.
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(collect,FEEDS))
    finally:
        with STATE_LOCK:
            STATE['running']=False
            STATE['last_finished']=now()
            STATE['next_run']=(datetime.now(timezone.utc)+timedelta(seconds=settings()['interval_seconds'])).isoformat()
        SYNC_LOCK.release()
    return True

def scheduler():
    while not STOP.is_set():
        started=time.monotonic()
        sync_all()
        delay=max(1,settings()['interval_seconds']-(time.monotonic()-started))
        with STATE_LOCK:
            STATE['next_run']=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
        WAKE.wait(delay)
        WAKE.clear()

def seed():
    with connect() as c:
        if c.execute('SELECT count(*) FROM notices').fetchone()[0]:
            return
    for feed in FEEDS:
        paths=sorted((ROOT/'probe-results').glob('gem-page-*.json')) if feed=='gem' else [ROOT/'probe-results'/f'{feed}.html']
        for path in paths:
            if not path.exists():
                continue
            try:
                records=parse_gem(json.loads(path.read_text(encoding='utf-8')))[0] if feed=='gem' else parse_gep(path.read_text(encoding='utf-8'),feed,FEEDS[feed][1])
                at=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(timespec='seconds')
                save_records(feed,records,at,'imported')
            except Exception:
                logging.exception('Could not import %s',path.name)

def query_notices(params, export=False, user=None):
    where=[]; args=[]
    table='notices'
    table_args=[]
    if user:
        # User id comes exclusively from the verified session, never request input.
        table="(SELECT n.*, EXISTS(SELECT 1 FROM user_bookmarks b WHERE b.notice_id=n.id AND b.user_id=?) AS user_saved FROM notices n)"
        table_args=[user['id']]
    if params.get('recommended',[''])[0]=='1' and user:
        terms=auth.terms(user)
        field="lower(coalesce(title,'')||' '||coalesce(organisation,'')||' '||coalesce(description,'')||' '||coalesce(category,'')||' '||reference)"
        where.append('('+' OR '.join('instr('+field+',?)>0' for _ in terms)+')' if terms else '0')
        args.extend(terms)
        where.append("deadline>? AND kind!='corrigendum'");args.append(now())
    q=params.get('q',[''])[0][:200]
    if q:
        # Treat user text literally rather than letting LIKE wildcards broaden it.
        term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        where.append("(title LIKE ? ESCAPE '\\' OR reference LIKE ? ESCAPE '\\' OR organisation LIKE ? ESCAPE '\\')")
        args.extend([term]*3)
    source=params.get('source',[''])[0]
    if source=='cppp':
        where.append("feed IN ('cppp','epublish')")
    elif source in FEEDS:
        where.append('feed=?'); args.append(source)
    kind=params.get('kind',[''])[0]
    if kind in ('tender','auction','corrigendum'):
        where.append('kind=?'); args.append(kind)
    status=params.get('status',[''])[0]
    if status in ('open','closing'):
        where.append('deadline>?'); args.append(now())
        if status=='closing':
            where.append('deadline<=?');args.append((datetime.now(timezone.utc)+timedelta(days=7)).isoformat())
    elif status=='closed':
        where.append('deadline<=?');args.append(now())
    if params.get('saved',[''])[0]=='1':
        where.append('user_saved=1' if user else 'bookmarked=1')
    clause=' WHERE '+' AND '.join(where) if where else ''
    sort={'closing':'deadline IS NULL, deadline ASC','latest':'first_seen DESC','updated':'updated_at DESC'}.get(params.get('sort',['latest'])[0],'first_seen DESC')
    page=max(1,min(100000,int(params.get('page',['1'])[0])))
    limit=10000 if export else 12
    with connect() as c:
        total=c.execute('SELECT count(*) FROM '+table+clause,table_args+args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT * FROM '+table+clause+' ORDER BY '+sort+',id LIMIT ? OFFSET ?',table_args+args+[limit,0 if export else (page-1)*limit])]
    if user:
        for row in rows:
            row['bookmarked']=row.pop('user_saved');row['match_terms']=auth.matches(row,user)
    return {'items':rows,'total':total,'page':page,'limit':limit}

def dashboard():
    t=now(); week=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
    with connect() as c:
        stats=dict(c.execute('''SELECT count(*) total,
          sum(CASE WHEN deadline>? AND kind!='corrigendum' THEN 1 ELSE 0 END) open,
          sum(CASE WHEN deadline>? AND deadline<=? AND kind!='corrigendum' THEN 1 ELSE 0 END) closing,
          sum(CASE WHEN kind='corrigendum' THEN 1 ELSE 0 END) corrigenda,
          sum(bookmarked) saved FROM notices''',(t,t,week)).fetchone())
        feeds=[dict(r) for r in c.execute('SELECT s.*,(SELECT count(*) FROM notices n WHERE n.feed=s.id) stored FROM sources s')]
        events=[dict(r) for r in c.execute('SELECT e.id,e.notice_id,e.event_type,e.at,n.title,n.feed,n.reference FROM events e JOIN notices n ON n.id=e.notice_id ORDER BY e.id DESC LIMIT 12')]
        # Detection times, not source publication dates.
        activity=[dict(r) for r in c.execute("SELECT date(at,'+5 hours','+30 minutes') day,count(*) count FROM events WHERE at>=? GROUP BY day",((datetime.now(timezone.utc)-timedelta(days=6)).replace(hour=0,minute=0,second=0).isoformat(),))]
        runs=[dict(r) for r in c.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 20')]
    with STATE_LOCK:
        state={**STATE,'current':list(STATE['current'])}
    return {'stats':{k:v or 0 for k,v in stats.items()},'sources':feeds,'events':events,'activity':activity,'runs':runs,'sync':state,'settings':settings(),'server_time':t}

class Handler(BaseHTTPRequestHandler):
    def session(self):
        with connect() as c:
            return auth.from_cookie(c,self.headers.get('Cookie'))
    def log_message(self,fmt,*args):
        logging.debug(fmt,*args)
    def send(self,status,body,ctype='application/json; charset=utf-8',extra=None):
        if not isinstance(body,bytes):
            body=json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k,v in (extra or {}).items():
            self.send_header(k,v)
        self.end_headers();self.wfile.write(body)
    def valid_host(self):
        return self.headers.get('Host','') in (f'127.0.0.1:{PORT}',f'localhost:{PORT}')
    def do_GET(self):
        if not self.valid_host():
            return self.send(403,{'error':'Local access only'})
        parsed=urlparse(self.path); path=parsed.path; params=parse_qs(parsed.query)
        try:
            session=self.session() if path.startswith('/api/') else None
            if path.startswith('/api/') and not session:
                return self.send(401,{'error':'Please sign in'})
            user=session[0] if session else None
            if path=='/api/me':
                return self.send(200,{'user':user,'domains':auth.DOMAINS})
            if path=='/api/dashboard':
                result=dashboard()
                with connect() as c:
                    result['stats']['saved']=c.execute('SELECT count(*) FROM user_bookmarks WHERE user_id=?',(user['id'],)).fetchone()[0]
                return self.send(200,result)
            if path=='/api/notices':
                return self.send(200,query_notices(params,user=user))
            if path.startswith('/api/documents/'):
                with connect() as c:
                    doc=c.execute('SELECT * FROM documents WHERE id=?',(path.rsplit('/',1)[-1],)).fetchone()
                if not doc:
                    return self.send(404,{'error':'Document not found'})
                return self.send(200,doc['body'],'application/pdf',{'Content-Disposition':f'attachment; filename="{doc["name"]}"'})
            if path=='/api/export':
                rows=query_notices(params,True,user)['items']; stream=io.StringIO(); writer=csv.writer(stream)
                cols=['feed','kind','title','reference','organisation','deadline','url']
                writer.writerow(cols)
                for row in rows:
                    values=[str(row.get(k) or '') for k in cols]
                    writer.writerow(["'"+v if v.startswith(('=','+','-','@','\t','\r')) else v for v in values])
                return self.send(200,('\ufeff'+stream.getvalue()).encode(),'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="tenderdesk-export.csv"'})
            if path.startswith('/api/notices/'):
                key=path.rsplit('/',1)[-1]
                with connect() as c:
                    row=c.execute('SELECT * FROM notices WHERE id=?',(key,)).fetchone()
                    if not row:
                        return self.send(404,{'error':'Notice not found'})
                    row=dict(row)
                    row['bookmarked']=int(c.execute('SELECT 1 FROM user_bookmarks WHERE user_id=? AND notice_id=?',(user['id'],key)).fetchone() is not None)
                    history=[dict(x) for x in c.execute('SELECT event_type,at,payload FROM events WHERE notice_id=? ORDER BY id DESC',(key,))]
                return self.send(200,{'notice':dict(row),'history':[{**h,'payload':json.loads(h['payload'])} for h in history]})
            files={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/session.js':('session.js','text/javascript'),'/styles.css':('styles.css','text/css')}
            if path in files:
                name,mime=files[path]
                return self.send(200,(ROOT/'web'/name).read_bytes(),mime+'; charset=utf-8')
            return self.send(404,{'error':'Not found'})
        except (ValueError,TypeError):
            self.send(400,{'error':'Invalid request'})
        except Exception:
            logging.exception('Request failed'); self.send(500,{'error':'Server error; see data/app.log'})
    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-TenderDesk')!='1':
            return self.send(403,{'error':'Request not allowed'})
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://localhost:{PORT}',f'http://127.0.0.1:{PORT}'):
            return self.send(403,{'error':'Origin not allowed'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=4096:
                return self.send(413,{'error':'Request too large'})
            body=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body,dict):
                raise ValueError('Invalid body')
            if self.path=='/api/login':
                if not isinstance(body.get('email'),str) or not isinstance(body.get('password'),str) or len(body['password'])>128:
                    raise ValueError('Invalid credentials')
                interests=body.get('interests');keywords=body.get('keywords','')
                if interests is not None and (not isinstance(interests,list) or len(interests)>10 or any(not isinstance(x,str) or x not in auth.DOMAINS for x in interests) or not isinstance(keywords,str) or len(keywords)>300):
                    raise ValueError('Invalid interests')
                with connect() as c:
                    result,error=auth.login(c,body['email'],body['password'])
                    if result and interests is not None:
                        c.execute('UPDATE users SET interests=?,keywords=? WHERE id=?',(json.dumps(interests),keywords,result[0]['id']))
                        result[0]['interests']=interests;result[0]['keywords']=keywords
                if error:
                    return self.send(401,{'error':error})
                return self.send(200,{'user':result[0]},extra={'Set-Cookie':'td_session='+result[1]+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
            session=self.session()
            if not session:
                return self.send(401,{'error':'Please sign in'})
            user=session[0]
            if self.path=='/api/logout':
                with connect() as c:
                    c.execute('DELETE FROM sessions WHERE id=?',(session[1],))
                return self.send(200,{'ok':True},extra={'Set-Cookie':'td_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            if self.path=='/api/profile':
                interests=body.get('interests');keywords=body.get('keywords','')
                if not isinstance(interests,list) or len(interests)>10 or any(not isinstance(x,str) or x not in auth.DOMAINS for x in interests) or not isinstance(keywords,str) or len(keywords)>300:
                    raise ValueError('Invalid interests')
                with connect() as c:
                    c.execute('UPDATE users SET interests=?,keywords=? WHERE id=?',(json.dumps(interests),keywords,user['id']))
                return self.send(200,{'ok':True})
            if self.path=='/api/documents':
                return self.send(200,fetch_documents(body.get('id','')))
            if self.path=='/api/sync':
                with STATE_LOCK:
                    running=STATE['running']
                if not running:
                    WAKE.set()
                return self.send(202,{'queued':not running,'running':running})
            if self.path=='/api/settings':
                limits={'interval_seconds':(30,86400),'gem_pages':(1,20),'gem_backfill_pages':(0,10)}
                values={}
                for k,(lo,hi) in limits.items():
                    if k in body:
                        if type(body[k]) is not int or not lo<=body[k]<=hi:
                            raise ValueError('Invalid setting')
                        values[k]=body[k]
                with connect() as c:
                    for k,v in values.items():
                        c.execute('UPDATE settings SET value=? WHERE key=?',(str(v),k))
                WAKE.set()
                return self.send(200,settings())
            if self.path=='/api/bookmark':
                if type(body.get('saved')) is not bool:
                    raise ValueError('Invalid saved flag')
                with connect() as c:
                    if not c.execute('SELECT 1 FROM notices WHERE id=?',(body.get('id',''),)).fetchone():
                        return self.send(404,{'error':'Notice not found'})
                    if body['saved']:
                        c.execute('INSERT OR IGNORE INTO user_bookmarks VALUES(?,?)',(user['id'],body['id']))
                    else:
                        c.execute('DELETE FROM user_bookmarks WHERE user_id=? AND notice_id=?',(user['id'],body['id']))
                return self.send(200,{'saved':body['saved']})
            self.send(404,{'error':'Not found'})
        except (ValueError,TypeError):
            self.send(400,{'error':'Invalid request'})

def main():
    logging.basicConfig(filename=DATA/'app.log',level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    init_db();seed()
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    worker=threading.Thread(target=scheduler,daemon=True);worker.start()
    print(f'TenderDesk running at http://127.0.0.1:{PORT}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set();WAKE.set();server.server_close()

if __name__=='__main__':
    main()
