"""Unified CLI for LinkMeddle REST API"""

import time
import random
import click
import lmaqcl

@click.group()
@click.option('-a', '--api', required=True, help='Base API URL')
@click.option('-q', '--quiet', is_flag=True)
@click.pass_context
def cli(ctx, api, quiet):
    """CLI for LM REST API"""
    ctx.ensure_object(dict)
    ctx.obj['api'] = lmaqcl.LinkMeddleClient(api)
    ctx.obj['quiet'] = quiet


@cli.command()
@click.argument('url', nargs=-1)
@click.option('-w', '--wait', type=int, help='check for resp every X secs')
@click.option('-b', '--backend', help='Specify a backend')
@click.option('-i', '--ignore-errors', is_flag=True)
@click.pass_context
def download(ctx, url, wait, backend, ignore_errors):
    """Download one or more URLs"""
    # TODO return error status if wait and failure
    # TODO more concise status reporting
    ids = [ctx.obj['api'].start_download(x,
                                         backend=backend,
                                         ignoreerrors=ignore_errors)
           for x in url]
    if not ctx.obj['quiet']:
        print('ID(s):')
        for myid in ids:
            print(myid)
        print()
    if not wait:
        return
    status = [{'state': 'PENDING'}]
    while [x for x in status if x.get('state') == 'PENDING']:
        time.sleep(wait)
        status = [ctx.obj['api'].download_detail(x) for x in ids]
        if not ctx.obj['quiet']:
            print('Status:')
            for mystat in status:
                print(mystat)
            print()


@cli.command()
@click.argument('filename', type=click.File('r'))
@click.option('-p', '--partial', type=int, help='Process a random 1/Nth of file')
@click.option('-i', '--ignore-errors-sometimes', is_flag=True,
              help='Pass ignore errors half the time')
@click.pass_context
def refresh(ctx, filename, partial, ignore_errors_sometimes):
    """Refresh all URLs in a given file"""
    urls = [line.rstrip('\n') for line in filename]
    if partial:
        full = len(urls)
        mypart = int(full/partial) + 1
        urls = random.sample(urls, mypart)
    for url in urls:
        ignoreerrors = False
        if ignore_errors_sometimes and bool(random.getrandbits(1)):
            ignoreerrors = True
        myid = ctx.obj['api'].start_download(url, ignoreerrors=ignoreerrors)
        if not ctx.obj['quiet']:
            print("{}\t{}".format(myid, url))


if __name__ == '__main__':
    cli(obj={})
