import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import app
import auth

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patcher=patch.object(app,'DB',Path(self.temp.name)/'test.db');self.patcher.start();app.init_db()
        with app.connect() as c:
            self.uid=auth.create_user(c,'one@example.test','One','Long-test-password',['railway'])
            self.other=auth.create_user(c,'two@example.test','Two','Long-test-password',['healthcare'])
            self.user=auth.profile(c.execute('SELECT * FROM users WHERE id=?',(self.uid,)).fetchone())
        app.save_records('gem',[dict(id='rail',feed='gem',kind='tender',title='Railway signalling',reference='REF',organisation='',category='',deadline='2099-01-01T00:00:00+00:00',opening=None,url='https://bidplus.gem.gov.in/showbidDocument/1',description='Track work')])
    def tearDown(self):
        self.patcher.stop();self.temp.cleanup()
    def test_jwt_tamper_expiry_and_revocation(self):
        with app.connect() as c:
            token=auth.issue(c,self.uid)
            self.assertEqual(auth.verify(c,token)[0]['id'],self.uid)
            self.assertIsNone(auth.verify(c,token+'x'))
            with patch('auth.time.time',return_value=9999999999):
                self.assertIsNone(auth.verify(c,token))
            c.execute('DELETE FROM sessions')
            self.assertIsNone(auth.verify(c,token))
    def test_personalized_matches_and_watchlist_isolation(self):
        with app.connect() as c:
            c.execute('INSERT INTO user_bookmarks VALUES(?,?)',(self.other,'rail'))
        self.assertEqual(app.query_notices({'saved':['1']},user=self.user)['total'],0)
        result=app.query_notices({'recommended':['1']},user=self.user)
        self.assertEqual(result['total'],1)
        self.assertIn('railway',result['items'][0]['match_terms'])
        self.user['interests']=['healthcare']
        self.assertEqual(app.query_notices({'recommended':['1']},user=self.user)['total'],0)
        with app.connect() as c:
            c.execute("UPDATE notices SET deadline='2000-01-01'")
        self.user['interests']=['railway']
        self.assertEqual(app.query_notices({'recommended':['1']},user=self.user)['total'],0)
    def test_http_login_authorization_logout(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        with patch.object(app,'PORT',server.server_port):
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(path,body=None,cookie=None):
                headers={'X-TenderDesk':'1'}
                if cookie:headers['Cookie']=cookie
                req=Request('http://127.0.0.1:'+str(server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
                return urlopen(req)
            try:
                for path in ['/api/notices','/api/dashboard','/api/documents/any']:
                    with self.assertRaises(HTTPError) as raised:request(path)
                    self.assertEqual(raised.exception.code,401)
                with request('/api/login',{'email':'one@example.test','password':'Long-test-password','interests':['electrical'],'keywords':'solar'}) as response:
                    cookie=response.headers['Set-Cookie'].split(';')[0]
                    self.assertIn('HttpOnly',response.headers['Set-Cookie'])
                with request('/api/me',cookie=cookie) as response:
                    user=json.load(response)['user']
                    self.assertEqual(user['id'],self.uid)
                    self.assertEqual(user['interests'],['electrical'])
                    self.assertEqual(user['keywords'],'solar')
                with request('/api/profile',{'interests':['railway','it'],'keywords':'signalling'},cookie) as response:self.assertEqual(response.status,200)
                with request('/api/me',cookie=cookie) as response:self.assertEqual(json.load(response)['user']['keywords'],'signalling')
                with request('/api/bookmark',{'id':'rail','saved':True},cookie) as response:self.assertEqual(response.status,200)
                with request('/api/notices?saved=1',cookie=cookie) as response:self.assertEqual(json.load(response)['total'],1)
                with request('/api/logout',{},cookie) as response:self.assertEqual(response.status,200)
                with self.assertRaises(HTTPError):request('/api/me',cookie=cookie)
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
