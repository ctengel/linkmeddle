#!/usr/bin/env python3

# NOTE this is essentially a sample

"""Download files from JSON"""


import argparse
import warnings
import json
import urllib
from posixpath import join as urljoin
import linkmeddle
#import requests_cache
#import datetime
#import time
#import requests
#import dohresolver
#import collections
#import random
#import re
#import os


QUAL = ['4K', 'High', 'Med', 'Low']

SICFG = None

def dl_one(indict, base, qual):
    """Try to dl video of one quality"""
    #file_name = dest + '/' + re.sub('[^\w.]', '_', base_name)
    #url = '{}{}'.format(baseurl, urllib.parse.quote(base_name))
    url = (base + indict['siteName'] + ' ' + indict['videoName'] + ' ' +
           qual + '.mp4')
    try:
        linkmeddle.download(url)
    except Exception as excpt:
        warnings.warn(str(excpt))
        return False
    return True


def proc_one(indict, base):
    """Get best available quality of video"""
    # TODO get thumbnail
    for qua in QUAL:
        if indict['version' + qua]:
            if dl_one(indict, base, qua.lower()):
                return True
    #assert False
    return False


def json_dl(jsonf, base):
    """Load json file and get all videos"""
    with open(jsonf) as fil:
        dic = json.load(fil)
    for item in dic:
        if not proc_one(item, base):
            print('FAILED {}'.format(item))


#def split_data(data, by='bonusGroup'):
#    result = collections.defaultdict(list)
#    for d in data:
#        result[d[by]].append(d)
#    return result
#def try_sub(subarea, base, dest, over=False):
#    total = 0
#    random.shuffle(subarea)
#    for i in subarea:
#        result = attempt_download(i, base, dest)
#        if not result and not total and not over:
#            return 0
#        if result == 1:
#            total = total + 1
#    return total
#def do_it(api, base, dest):
#    data = split_data(get_json(api))
#    total = 0
#    for k, v in data.items():
#        print(k)
#        num_dl = try_sub(v, base, dest)
#        total = total + num_dl
#    return total

def download(info):
    error = None
    mytype = info.get('type', 'playlist')
    url = info.get('url')
    url_split = urllib.parse.urlparse(url)
    url_scheme = url_split.scheme
    assert url_scheme in ['http', 'https']
    site_base = url_split.netloc
    url_path = url_split.path
    assert not url_split.params
    assert not url_split.query
    assert not url_split.fragment
    assert not url_split.username
    assert not url_split.password
    assert urlsplit.hostname == site_base
    assert not url_split.port
    if mytype == 'video':
        warnings.warn('SI: Not downloading {}'.format(url))
        details = info.get('details')
        assert details
        # TODO download one to proper place SICFG['DIR] . SICFG['VPATH']['PPATH']
        real_url = None
        return {'url': url,
                'backend': ['si', 'video'],
                'data': {'real_url': real_url,
                         'details': details,
                'error': error,
                'recurse': False,
                'retrieved': True,
                'type': mytype,
                'partial': False}
    assert mytype == 'playlist'
    recurse = info.get('recurse', 'async')
    assert recurse in ['async', False]  # sync and internal not supported yet
    # TODO allow passing in site instead of JSON
    vids = linkmeddle.get_json(url)
    # TODO save versioned/date JSON file in SICFG['DIR']
    entries = [{'url': urllib.parse.urlunsplit([url_scheme,
                                                site_base,
                                                urljoin(SICFG['PPATH'],
                                                        '{} {}.jpg'.format(x['siteName'], x['videoName']),
                                                None,
                                                None]),
                'backend': ['si', 'playlist'],
                'retrieved': False,
                'details': x} for x in vids]
    if entries and recurse == 'async':
        recurse_status = 'prepared'
    return {'url': url,
            'backend': ['si', 'playlist'],
            'data': {'entries': entries},
            'error': error,
            'recurse': recurse,
            'recurse_status': recurse_status,
            'type': mytype
            'partial': False}




def set_config(cfg_info):
    """Send in a dictionary with DIR, VPATH, and PPATH"""
    global SICFG
    SICFG = cfg_info.copy()

def backends():
    """Return available backends"""
    return {'download': download, 'set_config': set_config}

def _cli():
    parser = argparse.ArgumentParser(description='Download files from JSON')
    parser.add_argument('json')
    parser.add_argument('base')
    args = parser.parse_args()
    json_dl(args.json, args.base)


if __name__ == '__main__':
    _cli()
