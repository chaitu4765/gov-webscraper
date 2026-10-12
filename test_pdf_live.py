"""Manual read-only portal diagnostic; downloaded PDFs are saved by the app."""
import app
import re

if __name__ == '__main__':
    with app.connect() as c:
        rows=c.execute("SELECT id,feed,url FROM notices WHERE kind='tender' GROUP BY feed").fetchall()
    for row in rows:
        if row['feed']=='gem':
            continue
        client=app.Client()
        body=client.get(row['url'])
        parser=app.DocumentLinks(); parser.feed(body)
        print(row['feed'],[(url,label) for url,label in parser.links if re.search('pdf|download',url+' '+label,re.I)])
        for url,label in app.document_candidates(body,row['url']):
            pdf,final=client.binary(url)
            print(label, len(pdf), pdf[:160])
