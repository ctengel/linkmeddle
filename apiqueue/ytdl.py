"""Deal with youtube dl on local system as well as associated data structures in flat files"""

import json
import os
from yt_dlp import YoutubeDL
from yt_dlp.utils import locked_file, YoutubeDLError

TGTDIR = None
TGTAR = None

def _ydl(ignoreerrors=False):
    os.chdir(TGTDIR)
    # TODO user, password, cookiefile
    # TODO extract_flat:in_playlist, simulate, skip_download
    # TODO progress_hooks, quiet
    # TODO cachedir, restrictfilenames, nooverwrites, playlistrandom, auto_subtitles
    opts = {'writeinfojson': True,
            'download_archive': TGTAR,
            'writethumbnail': True,
            'writesubtitles': True,
            'sleep_interval': 4,
            'max_sleep_interval': 16,
            'ignoreerrors': ignoreerrors}
    return YoutubeDL(opts)

def download(info):
    """Do a download"""
    error = None
    url = info.get('url')
    # TODO broader support for new input format
    assert url
    ignoreerrors = info.get('ignoreerrors', False)
    with _ydl(ignoreerrors=ignoreerrors) as ydl:
        # TODO add way to not download or process
        try:
            retcode = ydl.extract_info(url)
        except YoutubeDLError as e:
            retcode = None
            error = str(e)
    if retcode:
        # TODO cleaner way to clean up unserializable objects
        retcode = json.loads(json.dumps(retcode, default=lambda o: repr(o)))
    # TODO broader support for new output format
    return {'url': url, 'data': retcode, 'error': error, 'ignoreerrors': ignoreerrors}


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
