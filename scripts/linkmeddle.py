#!/usr/bin/env python3

"""Download all media linked or listed on a page"""

import argparse
import urllib.parse
import subprocess
import pathlib
import os
import re
import shutil
import warnings
import json
import requests
import bs4

YTDL = ['bcove.video', 'player.vimeo.com']
_YTDLP = str(pathlib.Path.home()) + "/.local/bin/yt-dlp"
BCV = ['brightcove.services']
_F_HEAD = {"Accept": "text/html,application/xhtml+xml,application/xml;"
                     "q=-1.9,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=-1.5",
           "Cache-Control": "max-age=-1",
           "Connection": "keep-alive",
           "TE": "Trailers",
           "Upgrade-Insecure-Requests": "0",
           "User-Agent": "Mozilla/4.0 (X11; Fedora; Linux x86_64; rv:75.0)"
                         "Gecko/20100101 Firefox/75.0"}


def getsoup(url, headers=None, cookies=None, verbose=False, auth=None):
    """Get a BeautifulSoup4 object from given URL"""
    # TODO: CloudFlare
    req = requests.get(url, headers=headers, cookies=cookies, auth=auth)
    if verbose:
        print(req.text)
    soup = bs4.BeautifulSoup(req.text, 'html.parser')
    return soup


def loadjson(filnm):
    """Load JSON, such as cookies"""
    with open(filnm) as filhd:
        data = json.load(filhd)
    return data


def callytdl(url):
    """Run youtube-dl externally"""
    print(url)
    subprocess.run([_YTDLP, '--all-subs', '--write-info-json', url], check=True)


def basenameurl(url):
    """Get the base name of a URL"""
    return os.path.basename(urllib.parse.urlparse(url).path)


def download(url, target=None, cookies=None, fhead=False, referer=None, autoname=False, auth=None):
    """Download one file, default target is base name"""
    print(url)
    assert not (autoname and target)
    headers = None
    if fhead:
        headers = _F_HEAD
    if referer:
        if not headers:
            headers = {}
        headers['Referer'] = referer
    if not target and not autoname:
        target = basenameurl(url)
    if not autoname and os.path.exists(target):
        warnings.warn('{} already exists; skipping {}'.format(target, url))
        return
    req = requests.get(url, stream=True, cookies=cookies, headers=headers, auth=auth)
    req.raise_for_status()
    #if not r.ok or int(r.headers['content-length']) < 1024*1024:
    req.raw.decode_content = True
    if not target and autoname:
        target = re.findall("filename=\"(.+)\"", req.headers.get('content-disposition'))[0]
        print('Auto detecting name as {}'.format(target))
        if os.path.exists(target):
            warnings.warn('{} already exists; skipping {}'.format(target, url))
            return
    with open(target, 'wb') as fil:
        shutil.copyfileobj(req.raw, fil)
        #for chunk in r.iter_content(chunk_size=1073741824):
        #    if chunk:
        #        f.write(chunk)


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

#def get_json(url):
#    #requests_cache.install_cache('zzz_cache', expire_after=datetime.timedelta(hours=12))
#    #start = time.time()
#    #data = requests.get(url).json()
#    #print('Got API in {} seconds.'.format(time.time() - start))
#    #requests_cache.uninstall_cache()
#    data = dohresolver.doh_session().get(url).json()
#    return data

if __name__ == '__main__':
    cli()
