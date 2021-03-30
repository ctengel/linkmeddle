#!/usr/bin/env python3

"""Tools for handling video site"""

import re
import urllib.parse
import os.path
import warnings
import time
import subprocess
import linkmeddle


def activity(url):
    """Actual parsing of single URL"""
    soup = linkmeddle.getsoup(url)
    urls = [x.get('href') for x in soup.find_all('a')]
    absurls = [urllib.parse.urljoin(url, x) for x in urls]
    for href in absurls:
        parsed = urllib.parse.urlparse(href)
        matres = re.match(r'/video\d+/', parsed.path)
        if not matres:
            continue
        newhref = urllib.parse.urlunparse([parsed.scheme,
                                           parsed.netloc,
                                           parsed.path,
                                           None,
                                           None,
                                           None])
        try:
            print(newhref)
            #linkmeddle.callytdl(newhref)
        except subprocess.CalledProcessError as err:
            warnings.warn(str(err))
            time.sleep(30)
    divids = [x.get('id') for x in soup.find_all('div')]
    newtime = None
    for div in divids:
        if not div:
            continue
        matres = re.match(r'^activity-event-(\d+)$', div)
        if matres:
            newtime = matres.group(1)
    parsed = urllib.parse.urlparse(url)
    (base, timev) = os.path.split(parsed.path)
    if timev == 'activity':
        base = parsed.path
        timev = None
    if newtime and newtime != timev:
        newpath = os.path.join(base, newtime)
        activity(urllib.parse.urlunparse([parsed.scheme,
                                          parsed.netloc,
                                          newpath,
                                          None,
                                          None,
                                          None]))


if __name__ == '__main__':
    linkmeddle.cli(activity)
