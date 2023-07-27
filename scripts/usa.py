#!/usr/bin/env python3

import re
import bs4
import linkmeddle
import warnings

def soupfromfile(file):
    with open(file) as fh:
        var = fh.read()
    soup = bs4.BeautifulSoup(var, 'html.parser')
    return soup

def fromfile(url):
    print(url)
    soup = soupfromfile(url)
    hrefs = soup.find_all("a", class_="woocommerce-MyAccount-downloads-file button alt")
    for link in hrefs:
        fname = link.text
        murl = link.get('href')
        if re.search(r'-low.mp4$', fname):
            warnings.warn(f'Skipping {fname} because we think its low')
            continue
        linkmeddle.download(murl, fname)


if __name__ == '__main__':
    linkmeddle.cli(fromfile)
