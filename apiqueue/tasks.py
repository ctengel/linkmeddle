
import json
import os
from celery import Celery
from youtube_dl import YoutubeDL
from youtube_dl.utils import locked_file

app = Celery('tasks', backend='redis://', broker='pyamqp://')

TGTDIR = '/emergency/bla'
TGTAR = '/emergency/bla.txt'
# cachedir, user/password, download_archive, cookiefile, progress_hooks
OPTS = {'writeinfojson': True, 'download_archive': TGTAR}
#OPTS = {}

def _ydl():
    os.chdir(TGTDIR)
    return YoutubeDL(OPTS)

@app.task
def download(url, dl=False):
    with _ydl() as ydl:
        retcode = ydl.extract_info(url)
        return retcode

@app.task
def read_archive():
    with locked_file(TGTAR, 'r', encoding='utf-8') as archive_file:
        return [line.strip() for line in archive_file]

@app.task
def check_archive(dct):
    with _ydl() as ydl:
        return ydl.in_download_archive(dct)

@app.task
def add_archive(dct):
    with _ydl() as ydl:
        ydl.record_download_archive(dct)

@app.task
def lsdir():
    return os.listdir(TGTDIR)

@app.task
def infofiledata(fname):
    fullname = os.path.realpath(os.path.join(TGTDIR, fname))
    tgtreal = os.path.realpath(TGTDIR)
    assert os.path.commonpath((tgtreal, fullname)) == tgtreal
    with open(fullname) as fo:
        return json.load(fo)
