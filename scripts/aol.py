#!/usr/bin/env python3

"""Tools for handling books"""

import argparse
import time
import linkmeddle


def decode_json(metadata):
    """Return a flat dictionary based on JSON file input"""
    outdict = {}
    bid = metadata['data']['data']['id']
    bro = metadata['data']['brOptions']['data']
    for outer, outer_thing in enumerate(bro):
        for inner, inner_thing in enumerate(outer_thing):
            outdict[(bid, outer, inner)] = inner_thing['uri']
    return outdict


def dl_json(fname, cookfile=None, referer=None):
    """Download JSON"""
    metadata = linkmeddle.loadjson(fname)
    cookies = None
    if cookfile:
        cookies = linkmeddle.loadjson(cookfile)
    decoded = decode_json(metadata)
    for key, val in decoded.items():
        dlf = '-'.join(str(x) for x in key) + '.jpeg'
        print('{}\t{}'.format(dlf, val))
        linkmeddle.download(val, dlf, cookies, True, referer)
        time.sleep(2)


def _cli():
    parser = argparse.ArgumentParser('Download images file')
    parser.add_argument('json')
    parser.add_argument('-c', '--cookies')
    parser.add_argument('-r', '--referer')  # TODO parse this directly
    args = parser.parse_args()
    dl_json(args.json, args.cookies, args.referer)


if __name__ == '__main__':
    _cli()
