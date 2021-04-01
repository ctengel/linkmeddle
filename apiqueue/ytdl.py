"""Deal with youtube dl on local system as well as associated data structures in flat files"""

import json
import os
from youtube_dl import YoutubeDL
from youtube_dl.utils import locked_file

TGTDIR = None
TGTAR = None
# TODO cachedir, user/password, cookiefile, progress_hooks,dump-json, flat-playlist, dump-single, json, quiet???

def _ydl():
    os.chdir(TGTDIR)
    opts = {'writeinfojson': True, 'download_archive': TGTAR}
    return YoutubeDL(opts)

def download(info):
    """Do a download"""
    url = info.get('url')
    # TODO broader support for new input format
    assert url
    with _ydl() as ydl:
        # TODO add way to not download or process
        retcode = ydl.extract_info(url)
    # TODO broader support for new output format
    return {'url': url, 'data': retcode}


def read_archive():
    """Return whole ytdl archive"""
    with locked_file(TGTAR, 'r', encoding='utf-8') as archive_file:
        return [line.strip() for line in archive_file]

def check_archive(dct):
    """Check ytdl archive for one file"""
    with _ydl() as ydl:
        return ydl.in_download_archive(dct)

def add_archive(dct):
    """Add one file to ytdl archive"""
    with _ydl() as ydl:
        ydl.record_download_archive(dct)

def lsdir():
    """List download directory"""
    return os.listdir(TGTDIR)

def infofiledata(fname):
    """Details on one file"""
    fullname = os.path.realpath(os.path.join(TGTDIR, fname))
    tgtreal = os.path.realpath(TGTDIR)
    assert os.path.commonpath((tgtreal, fullname)) == tgtreal
    with open(fullname) as fo:
        return json.load(fo)

def newinfofile(fname, dct):
    """Import info file"""
    fullname = os.path.realpath(os.path.join(TGTDIR, fname))
    tgtreal = os.path.realpath(TGTDIR)
    assert os.path.commonpath((tgtreal, fullname)) == tgtreal
    assert not os.path.exists(fullname)
    with open(fullname, "w") as fh:
        json.dump(dct, fh)


def backends():
    # TODO add generic import, check, list functions
    return {'download': download}
