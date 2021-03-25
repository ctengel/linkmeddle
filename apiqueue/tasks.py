
import json
import os
from celery import Celery
import ytdl

app = Celery('tasks', backend='redis://', broker='pyamqp://')


@app.task
def download(url):
    return ytdl.download(url)

@app.task
def read_archive():
    return ytdl.read_archive()

@app.task
def check_archive(dct):
    return ytdl.check_archive(dct)

@app.task
def add_archive(dct):
    return ytdl.add_archive(dct)

@app.task
def lsdir():
    return ytdl.lsdir()

@app.task
def infofiledata(fname):
    return ytdl.infofiledata(fname)

@app.task
def newinfofile(fname, dct):
    return ytdl.newinfofile(fname, dct)
