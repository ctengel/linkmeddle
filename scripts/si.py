#!/usr/bin/env python3

"""Download files from JSON"""

import argparse
import warnings
import json
import linkmeddle

QUAL = ['4K', 'High', 'Med', 'Low']


def dl_one(indict, base, qual):
    """Try to dl video of one quality"""
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


def _cli():
    parser = argparse.ArgumentParser(description='Download files from JSON')
    parser.add_argument('json')
    parser.add_argument('base')
    args = parser.parse_args()
    json_dl(args.json, args.base)


if __name__ == '__main__':
    _cli()
