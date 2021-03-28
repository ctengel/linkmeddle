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
            trymkv = os.path.splitext(fn)[0] + '.mkv'
            if os.path.exists(trymkv) and os.path.getsize(trymkv) >= 1024:
                print('WARN: replacing {} with {}'.format(fn, trymkv))
                fn = trymkv
            else:
                print('Skipping {} fn since file not exist or zero...'.format(sp))
                continue
        print('Attempting to import {}...'.format(n))
        resp = requests.post(api, json={'sourcesys': None, 'sourcedir': dirpath, 'ijf': fd, 'ijfn': n, 'mediafile': fn})
        resp.raise_for_status()
        rj = resp.json()
        print('Result for {}:\t{}'.format(n, rj.get('result')))


if __name__ == '__main__':
    pullin()

