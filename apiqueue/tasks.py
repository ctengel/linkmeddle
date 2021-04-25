"""Celery wrappers for ytdl tasks"""

import os
from pathlib import Path
import uuid
import json
from celery import Celery
import ytdl
import vi
import si

BACKENDS = {'ytdl': ytdl.backends(), 'vi': vi.backends(), 'si': si.backends()}

os.environ.setdefault('CELERY_CONFIG_MODULE', 'celeryconfig')

app = Celery('tasks')

app.config_from_envvar('CELERY_CONFIG_MODULE')

for BCKND, BCKNDCT in BACKENDS.items():
    BCKNDF = BCKNDCT.get('set_config')
    if BCKNDF:
        BCKNDF(app.conf.get('LMB_' + BCKND.upper())

del BCKND
del BCKNDCT

ytdl.TGTDIR = app.conf.get('YTDL_DIR')
ytdl.TGTAR = app.conf.get('YTDL_ARC')

@app.task
def download(info):
    """Given a dictionary:
    url: ""
    backend: []
    recurse:
    ignoreerrors

    Do a download and log and return:
    url: ""
    data: "" (w/ entries - retrieved, jobid)
    error:
    recurse:
    recurse_status
    """

    # Preperation
    url = info.get('url')
    myback = info.get('backend', [None])[0]
    if not myback:
        # TODO auto detect others based on URL
        myback = 'ytdl'
        info['backend'] = [myback, None]
    # TODO more generic log name
    ldp = app.conf.get('YTDL_LOG')
    thisfile = None

    # Initial log
    if ldp:
        logdir = Path(ldp)
        # TODO use task ID instead??? or include in info at least???
        thisfile = logdir.joinpath(str(uuid.uuid1()))
        initiallog = info.copy()
        initiallog['error'] = None
        with thisfile.open('w') as filehand:
            json.dump(initiallog, filehand)

    # Run
    res = BACKENDS[myback]['download'](info)

    # Recurse
    if res.get('recurse') == 'async' and res.get('recurse_status') == 'prepared' and res.get('type') == 'playlist':
        for childtask in res['data']['entries']:
            cptask = childtask.copy()
            assert not cptask.pop('retrieved')
            newid = download.delay(cptask)
            childtask['jobid'] = newid.id
        res['recurse_status'] = 'submitted'

    # Results
    if url and not res.get('url'):
        res['url'] = url
    if not res.get('error'):
        res['error'] = None
    if thisfile:
        with thisfile.open('w') as filehand:
            json.dump(res, filehand)
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
