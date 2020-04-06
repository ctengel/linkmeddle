#!/usr/bin/env python3

"""Download all media linked or listed on a page"""

import argparse
import urllib.parse
import subprocess
import pathlib
import requests
import bs4

YTDL = ['bcove.video']
_YTDLP = str(pathlib.Path.home()) + "/.local/bin/youtube-dl"


def linkmeddle(url):
    """Actual parsing of single URL"""
    req = requests.get(url)
    soup = bs4.BeautifulSoup(req.text, 'html.parser')
    for link in soup.find_all('a'):
        href = link.get('href')
        parsed = urllib.parse.urlparse(href)
        if parsed.hostname in YTDL:
            print(href)
            subprocess.run([_YTDLP, '--all-subs', href], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Download all media linked or'
                                     'listed on a page')
    parser.add_argument('url', nargs='+', help='URLs to download')
    args = parser.parse_args()
    for aurl in args.url:
        linkmeddle(aurl)
