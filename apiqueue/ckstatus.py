"""Check status of downloads"""

import click
import lmaqcl

DEFAPI = 'http://127.0.0.1:5000/'

@click.command()
@click.option('-a', '--api', default=DEFAPI)
@click.option('-i', '--dlid')
def pullin(api, dlid):
    """Check status of all downloads or specific;

    output any not SUCCESS
    """
    if api.endswith('/download/'):
        api = api.rsplit('/', 2)[0] + '/'
    myapi = lmaqcl.LinkMeddleClient(api)
    ids = []
    if dlid:
        ids = [dlid]
    if not ids:
        ids = myapi.all_downloads()
    for myid in ids:
        r2 = myapi.download_detail(myid)
        if r2['state'] != 'SUCCESS':
            print(r2['url'])


if __name__ == '__main__':
    pullin()
