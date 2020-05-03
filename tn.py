#!/usr/bin/env python3

"""Tools for putting JSON together"""

import urllib.parse
import json
import argparse
import pprint
import requests


def urlupdate(url, page=None):
    """Update URL with desired page #"""
    parsed = urllib.parse.urlparse(url)
    psq = dict(urllib.parse.parse_qsl(parsed.query))
    pprint.pprint(psq)
    if page:
        psq['page[cursor]'] = page
    else:
        psq.pop('page[cursor]', None)
    pprint.pprint(psq)
    return urllib.parse.urlunparse([parsed.scheme,
                                    parsed.netloc,
                                    parsed.path,
                                    None,
                                    urllib.parse.urlencode(psq),
                                    None])


def _urlupdate_dirty(url, page=None):
    if not page:
        return url  # NOTE we don't check to make sure it doesn't have a page
    return url + '&page[cursor]=' + page


def recur(url, page=None):
    """Get URL from page and beyond"""
    url = _urlupdate_dirty(url, page)
    print(url)
    res = requests.get(url)  # TODO CloudFlare
    print(res.text)
    res.raise_for_status()
    raw = res.json()
    data = raw.get('data', list)
    page = raw.get('meta',
                   dict).get('pagination',
                             dict).get('cursors',
                                       dict).get('next')
    if page:
        data = data + recur(url, page)
    return data


def json_dl(url, outf):
    """Put entire unpaginated URL into out JSON file"""
    alldata = {'data': recur(url)}
    with open(outf, 'w') as outfile:
        json.dump(alldata, outfile)


def _cli():
    parser = argparse.ArgumentParser(description='Get contiguous JSON')
    parser.add_argument('url')
    parser.add_argument('json')
    args = parser.parse_args()
    json_dl(args.url, args.json)


if __name__ == '__main__':
    _cli()
