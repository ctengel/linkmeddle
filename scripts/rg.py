#!/usr/bin/env python3

import linkmeddle
import argparse
import urllib

def vidpg(aurl, auth):
    print(aurl)
    soup = linkmeddle.getsoup(aurl, auth=auth, verbose=False)
    ulz = soup.find_all('ul', class_='dropdown downloaddropdown')[0]
    liz = ulz.find_all('li')[1]
    az = liz.find_all('a')[0]
    linkmeddle.download(urllib.parse.urljoin(aurl, az.get('href')), auth=auth, ignore_nf=True)

def idxpg(aurl, auth):
    print(aurl)
    soup = linkmeddle.getsoup(aurl, auth=auth, verbose=False)
    divs = soup.find_all('div', class_='update_details')
    anchs = [div.find_all('a')[0] for div in divs]
    for anch in anchs:
        vidpg(anch.get('href'), auth)
    
def cli():
    """Run when called from CLI"""
    parser = argparse.ArgumentParser(description='Download all media linked or'
                                     'listed on a page')
    parser.add_argument('auth')
    parser.add_argument('url', nargs='+', help='URLs to download')
    args = parser.parse_args()
    auth = tuple(args.auth.split(':'))
    for aurl in args.url:
        idxpg(aurl, auth)

if __name__ == '__main__':
    cli()

