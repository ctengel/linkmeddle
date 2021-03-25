"""Load files from local system to REST API"""

import json
import os
import click
import requests

DEFAPI = 'http://127.0.0.1:5000/import'

@click.command()
@click.option('-a', '--api', default=DEFAPI)
@click.argument('dirpath')
def pullin(api, dirpath):
    for n in os.listdir(dirpath):
        if not n.endswith('.info.json'):
            continue
        with open(os.path.join(dirpath, n)) as f:
            fd = json.load(f)
        sp = fd.get('_filename')
        if not sp:
            print('Skipping {} since no filename...'.format(n))
            continue
        fn = os.path.join(dirpath, sp)
        if not os.path.exists(fn) or os.path.getsize(fn) < 1024:
            print('Skipping {} fn since file not exist or zero...'.format(sp))
            continue
        print('Attempting to import {}...'.format(n))
        resp = requests.post(api, json={'sourcesys': None, 'sourcedir': dirpath, 'ijf': fd, 'ijfn': n})
        resp.raise_for_status()
        rj = resp.json()
        print('Result for {}:\t{}'.format(n, rj.get('result')))


if __name__ == '__main__':
    pullin()

