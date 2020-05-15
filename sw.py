#!/usr/bin/env python3

"""Download best quality from email"""

import re
import argparse
import warnings
import pprint
import linkmeddle


def doit(txt):
    """Given email as text file, download best quality of all videos"""
    with open(txt) as fil:
        text = fil.read()
    vids = re.split(r'\~+', text)
    listing = {}
    for vid in vids:
        mo1 = re.search(r'\S+\.mp4', vid)
        mo2 = re.search(r'https\:\/\/\S+', vid)
        if mo1 and mo2:
            print('VIDEO: {} {}'.format(mo1.group(0), mo2.group(0)))
            listing[mo1.group(0)] = mo2.group(0)
        else:
            warnings.warn('Cannot parse {}'.format(vid))
    names = [x.rpartition('-') for x in listing]
    for name in names:
        assert name[1] == '-'
        if name[2] == 'low.mp4' and name[0] + '-HD.mp4' in listing:
            print('Skipping {} in favor of {}.'.format(name[0] + name[1] +
                                                       name[2],
                                                       name[0] + '-HD.mp4'))
            del listing[name[0] + name[1] + name[2]]
    pprint.pprint(listing)
    for key, value in listing.items():
        linkmeddle.download(value, key)


def _cli():
    """Run when called from CLI"""
    parser = argparse.ArgumentParser(description='Download HD from email')
    parser.add_argument('txt', help='text file')
    args = parser.parse_args()
    doit(args.txt)


if __name__ == '__main__':
    _cli()
