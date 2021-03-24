
import os
from celery import Celery
from youtube_dl import YoutubeDL

app = Celery('tasks', backend='redis://', broker='pyamqp://')

TGTDIR = '/emergency/bla'
OPTS = {'writeinfojson': True}


@app.task
def download(url):
    os.chdir(TGTDIR)
    with YoutubeDL(OPTS) as ydl:
        retcode = ydl.download([url])
        return bool(retcode == 0)
