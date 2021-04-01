"""Check status of downloads"""

import click
import requests

DEFAPI = 'http://127.0.0.1:5000/download/'

@click.command()
@click.option('-a', '--api', default=DEFAPI)
@click.option('-i', '--dlid')
def pullin(api, dlid):
    """Check status of all downloads or specific;

    output any not SUCCESS
    """
    ids = []
    if dlid:
        ids = [dlid]
    if not ids:
        resp = requests.get(api)
        resp.raise_for_status()
        ids = [x['id'] for x in resp.json()['downloads']]
    for myid in ids:
        r2 = requests.get(api + myid)
        r2.raise_for_status()
        if r2.json()['state'] != 'SUCCESS':
            print(r2.json()['url'])


if __name__ == '__main__':
    pullin()
