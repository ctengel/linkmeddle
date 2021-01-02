#!/usr/bin/env python3

"""Download best quality from email"""

import re
import argparse
import warnings
import pprint
import linkmeddle
import bs4

def doit(txt):
    """Given info as html file, download all videos"""
    with open(txt) as fil:
        text = fil.read()
    soup = bs4.BeautifulSoup(text, 'html.parser')
    links = [x.get('href') for x in soup.find_all('a', class_='button file-download')]
    for link in links:
        print(link)

def _cli():
    """Run when called from CLI"""
    parser = argparse.ArgumentParser(description='Download HD from email')
    parser.add_argument('txt', help='text file')
    args = parser.parse_args()
    doit(args.txt)


if __name__ == '__main__':
    _cli()
