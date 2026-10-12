"""Local-only HS256 JWT sessions, password hashing and interest matching."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie

DOMAINS = {
    'railway': ['railway', 'railways', 'railroad', 'locomotive', 'rolling stock', 'metro rail'],
    'construction': ['construction', 'civil works', 'building', 'renovation', 'road'],
    'it': ['software', 'computer', 'network', 'server', 'it services'],
    'healthcare': ['medical', 'hospital', 'surgical', 'pharmaceutical'],
    'defence': ['defence', 'defense', 'army', 'navy', 'air force'],
    'marine': ['ship', 'marine', 'vessel', 'dock'],
    'electrical': ['electrical', 'transformer', 'cable', 'generator', 'solar'],
}

def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,
      name TEXT NOT NULL,password_hash TEXT NOT NULL,interests TEXT NOT NULL,keywords TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS user_bookmarks(user_id TEXT REFERENCES users(id),
      notice_id TEXT REFERENCES notices(id),PRIMARY KEY(user_id,notice_id));
    CREATE TABLE IF NOT EXISTS auth_config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires INTEGER);
    CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT PRIMARY KEY,attempts INTEGER,until INTEGER);
    ''')
    c.execute('INSERT OR IGNORE INTO auth_config VALUES(?,?)',('jwt_secret',secrets.token_hex(32)))

def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    result=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()
    return salt+':'+result

def create_user(c,email,name,password,interests,keywords=''):
    if len(password)<12 or len(password)>128:
        raise ValueError('Password must contain 12–128 characters')
    uid=secrets.token_hex(16)
    c.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(uid,email.strip().lower(),name,password_hash(password),json.dumps(interests),keywords))
    return uid

def profile(row):
    return {k:json.loads(row[k]) if k=='interests' else row[k] for k in ('id','email','name','interests','keywords')}

def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()

def secret(c):
    return c.execute("SELECT value FROM auth_config WHERE key='jwt_secret'").fetchone()[0].encode()

def issue(c,uid):
    stamp=int(time.time()); sid=secrets.token_hex(24)
    c.execute('DELETE FROM sessions WHERE expires<=?',(stamp,))
    c.execute('INSERT INTO sessions VALUES(?,?,?)',(sid,uid,stamp+28800))
    header=b64(b'{"alg":"HS256","typ":"JWT"}')
    payload=b64(json.dumps(dict(sub=uid,jti=sid,iat=stamp,exp=stamp+28800,iss='tenderdesk',aud='local-dashboard')).encode())
    raw=header+'.'+payload
    return raw+'.'+b64(hmac.new(secret(c),raw.encode(),hashlib.sha256).digest())

def verify(c,token):
    try:
        if len(token)>4096: return None
        header,payload,sig=token.split('.')
        expected=b64(hmac.new(secret(c),(header+'.'+payload).encode(),hashlib.sha256).digest())
        if not hmac.compare_digest(sig,expected): return None
        decode=lambda value: json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if decode(header)!={'alg':'HS256','typ':'JWT'}: return None
        claims=decode(payload)
        if claims['exp']<=time.time() or claims['iss']!='tenderdesk' or claims['aud']!='local-dashboard': return None
        row=c.execute('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.id=? AND s.user_id=? AND s.expires>?',(claims['jti'],claims['sub'],time.time())).fetchone()
        return (profile(row),claims['jti']) if row else None
    except (ValueError,KeyError,TypeError):
        return None

def from_cookie(c,header):
    try:
        cookie=SimpleCookie();cookie.load(header or '')
        return verify(c,cookie['td_session'].value) if 'td_session' in cookie else None
    except Exception:
        return None

def login(c,email,password):
    # Local shared bucket also prevents trying an unlimited sequence of emails.
    stamp=int(time.time()); bucket='local'
    limit=c.execute('SELECT * FROM login_attempts WHERE bucket=?',(bucket,)).fetchone()
    if limit and limit['until']>stamp and limit['attempts']>=10:
        return None,'Too many attempts. Try again in 15 minutes.'
    if not limit or limit['until']<=stamp:
        c.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(bucket,0,stamp+900))
    c.execute('UPDATE login_attempts SET attempts=attempts+1 WHERE bucket=?',(bucket,))
    row=c.execute('SELECT * FROM users WHERE email=?',(email.strip().lower(),)).fetchone()
    stored=row['password_hash'] if row else 'missing:invalid'
    computed=password_hash(password,stored.split(':')[0])
    if not row or not hmac.compare_digest(stored,computed):
        return None,'Invalid email or password'
    c.execute('DELETE FROM login_attempts WHERE bucket=?',(bucket,))
    return (profile(row),issue(c,row['id'])),None

def terms(user):
    values=[term for domain in user['interests'] for term in DOMAINS.get(domain,[])]
    return list(dict.fromkeys(values+[x.strip().lower() for x in user['keywords'].split(',') if x.strip()]))

def matches(notice,user):
    text=' '.join(str(notice.get(k) or '') for k in ('title','organisation','description','category','reference')).lower()
    return [term for term in terms(user) if term in text]
