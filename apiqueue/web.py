
from flask import Flask, request
from celery.result import AsyncResult
import tasks

app = Flask(__name__)
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
    result = tasks.download.delay(url)
    downloads[result.id] = url
    return {'url': url, 'id': result.id}

@app.route('/download/<dlid>')
def onedl(dlid):
    res = AsyncResult(dlid, app=tasks.app)
    finres = {'state': res.state, 'url': downloads[dlid], 'result': None}
    if res.ready():
        finres['result'] = res.get()
    return finres
