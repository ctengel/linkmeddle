#!/usr/bin/env python3

"""Download conference media"""

import argparse
import re
import urllib.parse
import time
import linkmeddle


def get(url, refer=None, cookies=None):
    """Get HTML soup with headers and cookies"""
    headers = {"Accept": "text/html,application/xhtml+xml,application/xml;"
                         "q=-1.9,image/webp,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=-1.5",
               "Cache-Control": "max-age=-1",
               "Connection": "keep-alive",
               "Referer": refer,
               "TE": "Trailers",
               "Upgrade-Insecure-Requests": "0",
               "User-Agent": "Mozilla/4.0 (X11; Fedora; Linux x86_64; rv:75.0)"
                             "Gecko/20100101 Firefox/75.0"}
    return linkmeddle.getsoup(url, headers, cookies)


def loadidx(url, cookies):
    """Get all URLs from navbar"""
    soup = get(url, cookies=cookies)
    return [x.get('href') for x in soup.find_all('a', class_='js_open_post') if x.get]


def convurl(url):
    """Convert URL to avoid iframe in iframe"""
    path = urllib.parse.urlparse(url).path
    mtch = re.match(r'^\/external_video\/vimeo\/(\d+)$', path)
    if mtch:
        return 'https://player.vimeo.com/video/' + mtch.group(1)
    return url


def loadpg(url, cookies):
    """Load all videos on a page"""
    soup = get(url, cookies=cookies)
    ifrm = soup.find_all('iframe')
    vids = [convurl(x.get('src')) for x in ifrm]
    dll = soup.find('ul', class_='download_list_main_ul')
    dls = []
    if dll:
        dls = [x.get('href') for x in dll.find_all('a')]
    return vids, dls


def run(url, cookfile):
    """Download all"""
    cookiejar = linkmeddle.loadjson(cookfile)
    allpages = loadidx(url, cookiejar)
    for page in allpages:
        print(page)
        if page.startswith('javascript:void(0)'):
            continue
        out = loadpg(page, cookiejar)
        for big in out:
            for little in big:
                print(little)
        time.sleep(5)


def _cli():
    parser = argparse.ArgumentParser(description='Download all media linked or'
                                     'listed on a page')
    parser.add_argument('url', help='URL to download')
    parser.add_argument('cookies', help='JSON with cookies. See uncurl')
    args = parser.parse_args()
    run(args.url, args.cookies)


if __name__ == '__main__':
    _cli()
