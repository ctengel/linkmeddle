
import json
import os
from youtube_dl import YoutubeDL
from youtube_dl.utils import locked_file

TGTDIR = '/emergency/bla'
TGTAR = '/emergency/bla.txt'
# cachedir, user/password, download_archive, cookiefile, progress_hooks
OPTS = {'writeinfojson': True, 'download_archive': TGTAR}

def _ydl():
    os.chdir(TGTDIR)
    return YoutubeDL(OPTS)

def download(url):
    with _ydl() as ydl:
        # TODO add way to not download or process
        retcode = ydl.extract_info(url)
        return retcode

def read_archive():
    with locked_file(TGTAR, 'r', encoding='utf-8') as archive_file:
        return [line.strip() for line in archive_file]

def check_archive(dct):
    with _ydl() as ydl:
        return ydl.in_download_archive(dct)

def add_archive(dct):
    with _ydl() as ydl:
        ydl.record_download_archive(dct)

def lsdir():
    return os.listdir(TGTDIR)

def infofiledata(fname):
    fullname = os.path.realpath(os.path.join(TGTDIR, fname))
    tgtreal = os.path.realpath(TGTDIR)
    assert os.path.commonpath((tgtreal, fullname)) == tgtreal
    with open(fullname) as fo:
        return json.load(fo)

def newinfofile(fname, dct):
    fullname = os.path.realpath(os.path.join(TGTDIR, fname))
    tgtreal = os.path.realpath(TGTDIR)
    assert os.path.commonpath((tgtreal, fullname)) == tgtreal
    assert not os.path.exists(fullname)
    with open(fullname, "w") as fh:
        json.dump(dct, fh)
