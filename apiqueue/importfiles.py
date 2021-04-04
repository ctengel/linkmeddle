"""Load files from local system to REST API"""

import json
import os
import click
import lmaqcl 

DEFAPI = 'http://127.0.0.1:5000/'

@click.command()
@click.option('-a', '--api', default=DEFAPI)
@click.argument('dirpath')
def pullin(api, dirpath):
    """Look through a local directory for .info.json files and import/register with API ytdl backend"""
    if api.endswith('/import'):
        api = api.rsplit('/', 1)[0] + '/'
    myapi = lmaqcl.LinkMeddleClient(api)
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
        print('Result for {}:\t{}'.format(n, myapi.import_info(info=fd,
                                                               info_name=n,
                                                               media_name=fn,
                                                               localdir=dirpath))


if __name__ == '__main__':
    pullin()
