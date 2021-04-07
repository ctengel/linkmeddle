"""Tools for handling video site"""

import re
import urllib.parse
import os.path
import linkmeddle


def activity(url):
    """Actual parsing of single activity URL with pages"""
    soup = linkmeddle.getsoup(url)
    urls = [x.get('href') for x in soup.find_all('a')]
    absurls = [urllib.parse.urljoin(url, x) for x in urls]
    plenturls = []
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
        plenturls.append(newhref)
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
        plenturls = plenturls + activity(urllib.parse.urlunparse([parsed.scheme,
                                                                  parsed.netloc,
                                                                  newpath,
                                                                  None,
                                                                  None,
                                                                  None]))
    return plenturls


def best(url):
    """Actual parsing of best URL"""
    soup = linkmeddle.getsoup(url)
    urls = [x.get('href') for x in soup.find_all('a')]
    absurls = [urllib.parse.urljoin(url, x) for x in urls]
    plenturls = []
    for href in absurls:
        parsed = urllib.parse.urlparse(href)
        matres = re.match(r'/prof-video-click/', parsed.path)
        if not matres:
            continue
        newhref = urllib.parse.urlunparse([parsed.scheme,
                                           parsed.netloc,
                                           parsed.path,
                                           None,
                                           None,
                                           None])
        plenturls.append(newhref)
    return plenturls
    # TODO recurse into more pages


SUBPARSERS = {'profile_activity': activity,
              'profile_videos_best': best}


def download(info):
    """Do a download of parsed url"""
    error = None
    url = info.get('url')
    mytype = info.get('type', 'playlist')
    assert mytype == 'playlist'
    # TODO recurse here or upstream?
    recurse = info.get('recurse', True)
    # TODO actually honor recurse
    recurse = False
    # TODO attempt to parse actual page URL instead of xhr stuff
    assert url
    urls = urllib.parse.urlparse(url)
    assert urls
    # TODO allow others besides profiles...
    assert urls.path.startswith('/profiles/')
    subp = info.get('backend', [None, None])[1]
    if not subp and urls.path.endswith('/activity'):
        subp = 'profile_activity'
    if not subp and urls.path.endswith('/videos/best'):
        subp = 'profile_videos_best'
    assert subp
    # TODO use domain to determine ytdl subparser
    entries = [{'url': x, 'backend': ['ytdl', None]} for x in SUBPARSERS[subp](url)]
    # TODO is data-entries the right way to do this?
    return {'url': url,
            'backend': ['vi', subp],
            'data': {'entries': entries},
            'error': error,
            'recurse': recurse,
            'type': mytype,
            'partial': subp != 'profile_videos_best'}


def backends():
    """Return available backends"""
    return {'download': download}
