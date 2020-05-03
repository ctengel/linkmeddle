#!/usr/bin/env python3

"""Download all media linked or listed on a page"""

import argparse
import urllib.parse
import subprocess
import pathlib
import os
import shutil
import requests
import bs4

YTDL = ['bcove.video', 'player.vimeo.com']
_YTDLP = str(pathlib.Path.home()) + "/.local/bin/youtube-dl"
BCV = ['brightcove.services']


def getsoup(url):
    """Get a BeautifulSoup4 object from given URL"""
    # TODO: CloudFlare
    req = requests.get(url)
    soup = bs4.BeautifulSoup(req.text, 'html.parser')
    return soup


def callytdl(url):
    """Run youtube-dl externally"""
    print(url)
    subprocess.run([_YTDLP, '--all-subs', url], check=True)


def basenameurl(url):
    """Get the base name of a URL"""
    return os.path.basename(urllib.parse.urlparse(url).path)


def download(url, target=None):
    """Download one file, default target is base name"""
    print(url)
    if not target:
        target = basenameurl(url)
    req = requests.get(url, stream=True)
    req.raise_for_status()
    req.raw.decode_content = True
    with open(target, 'wb') as fil:
        shutil.copyfileobj(req.raw, fil)


def bcv(url):
    """Process some special players"""
    print(url)
    parsed = urllib.parse.urlparse(url)
    bcv_id = urllib.parse.parse_qs(parsed.query)['videoId'][0]
    soup = getsoup(url)
    vidjs = soup.find('video-js')
    newpath = '/{}/{}_{}/index.html'.format(vidjs['data-account'],
                                            vidjs['data-player'],
                                            vidjs['data-embed'])
    newquery = urllib.parse.urlencode({'videoId': bcv_id})
    newurl = urllib.parse.urlunparse(['https',
                                      'players.brightcove.net',
                                      newpath,
                                      None,
                                      newquery,
                                      None])
    callytdl(newurl)


def linkmeddle(url):
    """Actual parsing of single URL"""
    soup = getsoup(url)
    urls = ([x.get('href') for x in soup.find_all('a')] +
            [x.get('src') for x in soup.find_all('iframe')])
    absurls = [urllib.parse.urljoin(url, x) for x in urls]
    for href in absurls:
        parsed = urllib.parse.urlparse(href)
        if not parsed.hostname:
            continue
        if parsed.hostname in YTDL:
            callytdl(href)
        else:
            for bcv_end in BCV:
                if parsed.hostname.endswith(bcv_end):
                    bcv(href)


def cli(fnc=None):
    """Run when called from CLI"""
    if fnc is None:
        fnc = linkmeddle
    parser = argparse.ArgumentParser(description='Download all media linked or'
                                     'listed on a page')
    parser.add_argument('url', nargs='+', help='URLs to download')
    args = parser.parse_args()
    for aurl in args.url:
        fnc(aurl)


if __name__ == '__main__':
    cli()
