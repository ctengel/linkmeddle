#!/usr/bin/env python3


"""Download files from JSON"""


import argparse
import warnings
import json
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
#import urllib


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
    mytype = info.get('type', 'playlist')
    url = info.get('burl')
    if mytype == 'video':
        # TODO download one to proper place
        # what should URL be???
        return {}
    assert mytype == 'playlist'
    recurse = info.get('recurse', 'async')
    assert recurse in ['async', False]  # sync and internal not supported yet
    # TODO save versioned/dated JSON file
    # TODO allow passing in site instead of JSON
    vids = linkmeddle.get_json(url)
    # TODO add passback info and return


def set_config(cfg_info):
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
