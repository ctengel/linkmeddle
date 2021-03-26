
from flask import Flask, request, abort, render_template, jsonify
from celery.result import AsyncResult
from youtube_dl.utils import YoutubeDLError
import tasks

app = Flask(__name__)
# TODO persist downloads and urls and playlist info
downloads = {}


@app.route('/download/', methods=['POST', 'GET'])
def download():
    url = None
    if request.json and 'url' in request.json:
        url = request.json['url']
    elif request.form and 'url' in request.form:
        url = request.form['url']
    elif request.args and 'url' in request.args:
        url = request.args['url']
    else:
        return jsonify({'downloads': [{'id': key, 'url': value} for key, value in downloads.items()]})
    # TODO validate if URL recently downloaded
    # TODO attempt to parse basic info
    result = tasks.download.delay(url)
    downloads[result.id] = url
    if request.args.get('fmt') == 'html' or request.form.get('fmt') == 'html':
        return render_template('submit.html', dlid=result.id)
    return jsonify({'url': url, 'id': result.id})

@app.route('/download/<dlid>')
def onedl(dlid):
    res = AsyncResult(dlid, app=tasks.app)
    finres = {'id': dlid, 'state': res.state, 'url': downloads.get(dlid), 'result': None, 'error': None}
    if res.ready():
        try:
            finres['result'] = res.get()
        except YoutubeDLError as e:
            finres['result'] = False
            finres['error'] = str(e)
    #if request.args.get('fmt') == 'html':
    #    return render_template("check.html", finres=finres)
    return jsonify(finres)

@app.route('/finished/')
def finished():
    # TODO this endpoint needs to make way more sense
    dirlist = tasks.lsdir.delay()
    arclist = tasks.read_archive.delay()
    return jsonify({'files': dirlist.get(), 'archive': arclist.get()})

@app.route('/finished/<fname>')
def details(fname):
    return jsonify(tasks.infofiledata.delay(fname).get())

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
        return jsonify({'result': 'exists'})
    if inlist:
        return jsonify({'result': 'fexists'})
    if inarch:
        return jsonify({'result': 'aexists'})
    aadd = tasks.add_archive.delay(mydata)
    fadd = tasks.newinfofile.delay(myname, mydata)
    assert fadd.get() is None
    assert aadd.get() is None
    return jsonify({'result': 'success'})

@app.route('/start')
def homepg():
    return render_template('start.html')
