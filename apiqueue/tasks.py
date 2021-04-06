"""Celery wrappers for ytdl tasks"""

import os
from pathlib import Path
import uuid
import json
from celery import Celery
import ytdl

BACKENDS = {'ytdl': ytdl.backends()}

os.environ.setdefault('CELERY_CONFIG_MODULE', 'celeryconfig')

app = Celery('tasks')

app.config_from_envvar('CELERY_CONFIG_MODULE')

ytdl.TGTDIR = app.conf.get('YTDL_DIR')
ytdl.TGTAR = app.conf.get('YTDL_ARC')

@app.task
def download(info):
    """Given a dictionary:
    url: ""
    backend: []

    Do a download and log and return:
    url: ""
    data: ""
    """
    url = info.get('url')
    myback = info.get('backend', [None])[0]
    if not myback:
        myback = 'ytdl'
        info['backend'] = [myback, None]
    # TODO more generic log name
    ld = app.conf.get('YTDL_LOG')
    thisfile = None
    if ld:
        logdir = Path(ld)
        # TODO use task ID instead??? or include in info at least???
        thisfile = logdir.joinpath(str(uuid.uuid1()))
        initiallog = info.copy()
        initiallog['error'] = None
        with thisfile.open('w') as fh:
            json.dump(initiallog, fh)
    res = BACKENDS[myback]['download'](info)
    # TODO spawn child tasks
    if url and not res.get('url'):
        res['url'] = url
    if not res.get('error'):
        res['error'] = None
    if thisfile:
        with thisfile.open('w') as fh:
            json.dump(res, fh)
    return res

@app.task
def read_archive():
    """DEPRECATED: read ytdl archive"""
    return ytdl.read_archive()

@app.task
def check_archive(dct):
    """DEPRECATED: list ytdl archive"""
    return ytdl.check_archive(dct)

@app.task
def add_archive(dct):
    """DEPRECATED: add info to ytdl archive"""
    return ytdl.add_archive(dct)

@app.task
def lsdir():
    """List a backend directory"""
    return ytdl.lsdir()

@app.task
def infofiledata(fname):
    """Get single metadata"""
    return ytdl.infofiledata(fname)

@app.task
def newinfofile(fname, dct):
    """Add new metadata"""
    return ytdl.newinfofile(fname, dct)
