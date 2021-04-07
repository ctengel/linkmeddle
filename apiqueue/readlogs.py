"""Look at local log files"""

from pathlib import Path
import datetime
import uuid
import json
import collections
#import csv
import click


def pulldirinfo(tdr):
    """Pull info on log files and media in a given directory"""
    outlist = []
    for item in tdr.iterdir():
        luuid = uuid.UUID(item.name)
        mtime = datetime.datetime.fromtimestamp(item.stat().st_mtime)
        with item.open() as f:
            info = json.load(f)
        # TODO add support for non-url
        url = info.get('url')
        backend = info.get('backend', ['ytdl', None])
        data = info.get('data')
        error = info.get('error')
        ok = (bool(data) and not bool(error))
        if not data:
            data = {}
        if not backend[1]:
            backend[1] = data.get('extractor_key')
        mytyp = data.get('_type')
        outlist.append({'logid': luuid,
                        'updated': mtime,
                        'url': url,
                        'backend': backend,
                        'success': ok,
                        'error': error,
                        'type': mytyp})
    return outlist

def greplogs(logf, whendct):
    """Grep a log file for interesting minutes"""
    greps = collections.defaultdict(list)
    result = {k: [] for k in whendct.keys()}
    for k,v in whendct:
        for thismin in [-1, 0, 1]:
            # TODO determine string value from v + thismin
            minstring = v + thismin
            greps[minstring].append(k)
    # TODO verify proper sort or do string conversion here...
    actual_greps = sorted(greps.keys())
    currentgrep = None
    with logf.open():
        # TODO intelligent grepping
        assert actual_greps
        fullline = ''
        # if found...
        for tgt in greps[currentgrep]:
            result[tgt].append(fullline)
    return fullline

def logtimes(dirdata):
    """determine the times when failures may have occured"""
    return {x['logid']: x['updated'] for x in dirdata if not x['success']}

def addlogdetails(dirdata, logfile):
    """try to add failure details from log"""
    times = logtimes(dirdata)
    details = greplogs(logfile, times)
    for x in dirdata:
        x['logdetails'] = details[x['logid']]

# TODO attempt to read flask.log ?

# TODO analysis such as output playlists only
# TODO output current failures (i.e. if it failed before but works now don't output)


@click.command()
@click.argument('dirname')
@click.argument('cellog')
@click.argument('outcsv')
def logs2csv(dirname, cellog, outcsv):
    """Read a local log directory and log file and output a CSV"""
    print('Reading directory...')
    mydirinfo = pulldirinfo(Path(dirname))
    for x in mydirinfo:
        print(x)

if __name__ == '__main__':
    logs2csv()
