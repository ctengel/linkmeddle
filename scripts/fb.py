#!/usr/bin/env python3

"""Get links from DOM"""

import argparse
import urllib.parse
import warnings
import bs4

def getsoup(txt):
    """Get a BeautifulSoup4 object from given URL"""
    with open(txt) as fh:
        soup = bs4.BeautifulSoup(fh, 'html.parser')
    return soup


def basenameurl(url):
    """Get the base name of a URL"""
    return os.path.basename(urllib.parse.urlparse(url).path)


def fbscan(soup):
    return [x.get('href') for x in soup.find_all('span')]

def cli():
    """Run when called from CLI"""
    parser = argparse.ArgumentParser(description='get all URLs')
    parser.add_argument('txt', help='html file')
    args = parser.parse_args()
    urls = fbscan(getsoup(args.txt))
    for url in urls:
        if not url:
            continue
        if 'https' not in url:
            continue
        print(url.partition('?')[0])

if __name__ == '__main__':
    cli()
