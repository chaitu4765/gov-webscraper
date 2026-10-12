"""Provision a local account without exposing its password in shell arguments."""
import argparse
import getpass
import secrets
import app
import auth

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--email',required=True)
    parser.add_argument('--name',default='TenderDesk user')
    parser.add_argument('--domain',choices=list(auth.DOMAINS),default='railway')
    parser.add_argument('--generate-password',action='store_true')
    args=parser.parse_args()
    password=secrets.token_urlsafe(15) if args.generate_password else getpass.getpass('Password (12+ characters): ')
    app.init_db()
    with app.connect() as c:
        auth.create_user(c,args.email,args.name,password,[args.domain])
    print('Created account:',args.email)
    if args.generate_password:
        print('Password:',password)
