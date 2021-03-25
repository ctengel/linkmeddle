
from flask import Flask, request, abort
from celery.result import AsyncResult
from youtube_dl.utils import YoutubeDLError
import tasks

app = Flask(__name__)
# TODO persist downloads and urls
downloads = {}


@app.route('/download/', methods=['POST', 'GET'])
def download():
    url = None
    content = request.json
    if request.json and 'url' in request.json:
        url = request.json['url']
    elif request.args and 'url' in request.args:
        url = request.args['url']
    else:
        return {'downloads': [{'id': key, 'url': value} for key, value in downloads.items()]}
    # TODO validate if URL recently downloaded
    # TODO attempt to parse basic info
    result = tasks.download.delay(url)
    downloads[result.id] = url
    return {'url': url, 'id': result.id}

@app.route('/download/<dlid>')
def onedl(dlid):
    if dlid not in downloads:
        abort(404)
    res = AsyncResult(dlid, app=tasks.app)
    finres = {'state': res.state, 'url': downloads[dlid], 'result': None, 'error': None}
    if res.ready():
        try:
            finres['result'] = res.get()
        except YoutubeDLError as e:
            finres['result'] = False
            finres['error'] = str(e)
    return finres

@app.route('/finished/')
def finished():
    # TODO endpoint way more sense
    dirlist = tasks.lsdir.delay()
    arclist = tasks.read_archive.delay()
    return {'files': dirlist.get(), 'archive': arclist.get()}

@app.route('/finished/<fname>')
def details(fname):
    return tasks.infofiledata.delay(fname).get()

@app.route('/import', methods=['POST'])
def importfiles():
    assert request.json
    assert request.json.get('ijf')
    assert request.json.get('ijfn')
    mydata = request.json.get('ijf')
    myname = request.json.get('ijfn')
    listdelay = tasks.lsdir.delay()
    checkarchive = tasks.check_archive.delay(mydata)
    inlist = (myname in listdelay.get())
    inarch = checkarchive.get()
    if inlist and inarch:
        return {'result': 'exists'}
    if inlist:
        return {'result': 'fexists'}
    if inarch:
        return {'result': 'aexists'}
    aadd = tasks.add_archive.delay(mydata)
    fadd = tasks.newinfofile.delay(myname, mydata)
    assert fadd.get() is None
    assert aadd.get() is None
    return {'result': 'success'}



# TODO more state internal like logging playlists or no??
