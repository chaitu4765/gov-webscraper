import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(app,'DB',Path(self.temp.name)/'test.sqlite3')
        self.db_patch.start();app.init_db()
        self.r=dict(id='one',feed='cppp',kind='tender',title='Test 100% contract',reference='REF_1',organisation='Ministry',category='',deadline='2026-11-01T00:00:00+00:00',opening=None,url='https://eprocure.gov.in/eprocure/app?session=one',description='Original')
    def tearDown(self):
        self.db_patch.stop();self.temp.cleanup()
    def test_idempotent_refresh_and_session_url_change(self):
        self.assertEqual(app.save_records('cppp',[self.r]),(1,0))
        self.r['url']='https://eprocure.gov.in/eprocure/app?session=two'
        self.assertEqual(app.save_records('cppp',[self.r]),(0,0))
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],1)
    def test_deadline_revision_preserves_bookmark_and_history(self):
        app.save_records('cppp',[self.r])
        with app.connect() as c:
            c.execute('UPDATE notices SET bookmarked=1')
        self.r['deadline']='2026-12-01T00:00:00+00:00'
        self.assertEqual(app.save_records('cppp',[self.r]),(0,1))
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT bookmarked FROM notices').fetchone()[0],1)
            rows=c.execute('SELECT payload FROM events ORDER BY id').fetchall()
            self.assertEqual(len(rows),2)
            self.assertNotEqual(json.loads(rows[0][0])['deadline'],json.loads(rows[1][0])['deadline'])
    def test_homepage_rank_is_not_an_amendment(self):
        self.r['description']='1. Same notice text'
        app.save_records('cppp',[self.r])
        self.r['description']='4. Same notice text'
        self.assertEqual(app.save_records('cppp',[self.r]),(0,0))
    def test_search_is_literal_and_saved_filter(self):
        app.save_records('cppp',[self.r])
        self.assertEqual(app.query_notices({'q':['%']})['total'],1)
        self.assertEqual(app.query_notices({'q':['nonexistent%']})['total'],0)
        self.assertEqual(app.query_notices({'saved':['1']})['total'],0)
    def test_source_failure_keeps_archive(self):
        app.save_records('cppp',[self.r])
        with patch.object(app.Client,'get',side_effect=ValueError('blocked')):
            app.collect('cppp')
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM notices').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT state FROM sources WHERE id='cppp'").fetchone()[0],'error')
    def test_date_conversion(self):
        self.assertEqual(app.date_value('21-Oct-2026 11:00 AM'),'2026-10-21T05:30:00+00:00')
        self.assertIsNone(app.date_value('invalid'))
    def test_seconds_default(self):
        self.assertEqual(app.settings()['interval_seconds'],30)
        self.assertNotIn('interval_minutes',app.settings())
    def test_document_discovery_and_cache(self):
        app.save_records('cppp',[self.r])
        html=b'<a href="/tender.pdf">Tender PDF</a><a href="https://evil.example/a.pdf">Bad</a>'
        with patch.object(app.Client,'binary',side_effect=[(html,self.r['url']),(b'%PDF-1.4 test','https://eprocure.gov.in/tender.pdf')]) as fetch:
            result=app.fetch_documents('one')
            self.assertEqual(len(result['documents']),1)
            self.assertEqual(fetch.call_count,2)
            self.assertEqual(app.fetch_documents('one')['documents'],result['documents'])
            self.assertEqual(fetch.call_count,2)
        with app.connect() as c:
            self.assertTrue(c.execute('SELECT body FROM documents').fetchone()[0].startswith(b'%PDF-'))
    def test_html_is_not_saved_as_pdf(self):
        app.save_records('cppp',[self.r])
        with patch.object(app.Client,'binary',side_effect=[(b'<a href="/a.pdf">PDF</a>',self.r['url']),(b'<html>CAPTCHA</html>',self.r['url'])]):
            self.assertEqual(app.fetch_documents('one')['documents'],[])
    def test_document_redirect_rejected(self):
        with self.assertRaises(ValueError):
            app.SafeRedirect().redirect_request(None,None,302,'',{},'http://127.0.0.1/private')
    def test_source_url_allowlist(self):
        self.assertFalse(app.allowed_url('javascript:alert(1)'))
        self.assertFalse(app.allowed_url('https://eprocure.gov.in.evil.example/test'))
        self.assertTrue(app.allowed_url('https://eprocurehsl.nic.in/nicgep/app'))
    def test_public_links_remove_session_and_preserve_identifier(self):
        from urllib.parse import parse_qs,urlparse
        original='https://eprocure.gov.in/epublish/app?component=%24DirectLink&page=Home&service=direct&session=T&sp=Sabc%2Bdef%3D'
        fixed=app.public_notice_url(original)
        query=parse_qs(urlparse(fixed).query)
        self.assertNotIn('session',query)
        self.assertEqual(query['sp'],['Sabc+def='])
        self.assertEqual(app.public_notice_url(fixed),fixed)
        self.assertEqual(app.public_notice_url(original.replace('page=Home','page=FrontEndTenderDetails')),original.replace('page=Home','page=FrontEndTenderDetails'))
    def test_startup_repairs_stored_notice_and_history(self):
        original='https://defproc.gov.in/nicgep/app?component=%24DirectLink_0&page=Home&service=direct&session=T&sp=Sabc'
        app.save_records('cppp',[self.r])
        with app.connect() as c:
            c.execute('UPDATE notices SET url=?',(original,))
            payload={**self.r,'url':original}
            c.execute('UPDATE events SET payload=?',(json.dumps(payload),))
        app.init_db()
        with app.connect() as c:
            self.assertNotIn('session=',c.execute('SELECT url FROM notices').fetchone()[0])
            self.assertNotIn('session=',json.loads(c.execute('SELECT payload FROM events').fetchone()[0])['url'])


class FixtureTests(unittest.TestCase):
    def test_gep_fixtures(self):
        for feed,expected in [('cppp',20),('epublish',20),('defproc',20),('hsl',18)]:
            path=app.ROOT/'probe-results'/f'{feed}.html'
            if not path.exists():
                continue
            rows=app.parse_gep(path.read_text(encoding='utf-8'),feed,app.FEEDS[feed][1])
            self.assertEqual(len(rows),expected)
            self.assertEqual(sum(r['kind']=='corrigendum' for r in rows),10)
            self.assertTrue(all(r['deadline'] for r in rows))
            self.assertEqual(len({r['id'] for r in rows}),len(rows))
    def test_gem_fixture_and_link_type(self):
        path=app.ROOT/'probe-results/gem-page-1.json'
        if not path.exists():
            self.skipTest('Probe fixture unavailable')
        records,total=app.parse_gem(json.loads(path.read_text(encoding='utf-8')))
        self.assertEqual(len(records),10)
        self.assertGreaterEqual(total,10)
        self.assertEqual(records[0]['kind'],'auction')
        self.assertIn('/showradocumentPdf/',records[0]['url'])
        self.assertTrue(records[0]['organisation'])


if __name__=='__main__':
    unittest.main()
