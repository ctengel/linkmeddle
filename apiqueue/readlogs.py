"""Look at local log files"""

from pathlib import Path
import datetime
import uuid
import json
import collections
import csv
import click


def pulldirinfo(tdr):
    """Pull info on log files and media in a given directory"""
    outlist = []
    for item in tdr.iterdir():
        luuid = uuid.UUID(item.name)
        mtime = datetime.datetime.fromtimestamp(item.stat().st_mtime)
        with item.open() as file_hand:
            info = json.load(file_hand)
        # TODO add support for non-url
        url = info.get('url')
        backend = info.get('backend', ['ytdl', None])
        data = info.get('data')
        error = info.get('error')
        ignoreerrors = info.get('ignoreerrors')
        dl_ok = (bool(data) and not bool(error))
        if not data:
            data = {}
        if not backend[1]:
            backend[1] = data.get('extractor_key')
        mytyp = data.get('_type')
        outlist.append({'logid': luuid,
                        'updated': mtime,
                        'url': url,
                        'backend': backend,
                        'success': dl_ok,
                        'error': error,
                        'type': mytyp,
                        'ignoreerrors': ignoreerrors})
    return outlist

def greplogs(logf, whendct):
    """Grep a log file for interesting minutes"""
    greps = collections.defaultdict(set)
    result = {k: [] for k in whendct.keys()}
    for key, val in whendct.items():
        for thismin in [-8, 0, 8]:
            newtime = val + datetime.timedelta(seconds=thismin)
            minstring = newtime.isoformat(sep=' ')[0:18]
            greps[minstring].add(key)
    with logf.open() as myfh:
        while True:
            fulline = myfh.readline()
            if not fulline:
                break
            for tgt in greps[fulline[1:19]]:
                result[tgt].append(fulline.strip())
    return result

def logtimes(dirdata):
    """determine the times when failures may have occured"""
    return {x['logid']: x['updated'] for x in dirdata if not x['success']}

def addlogdetails(dirdata, logfile):
    """try to add failure details from log"""
    times = logtimes(dirdata)
    details = greplogs(logfile, times)
    for myitem in dirdata:
        myitem['logdetails'] = details.get(myitem['logid'])

# TODO attempt to read flask.log ?



@click.command()
@click.argument('dirname')
@click.option('-l', '--log-file', help="Celery log file")
@click.option('-c', '--csv-file', help="CSV output file")
@click.option('-s', '--success', help="Success output file (pl only)")
@click.option('-f', '--failure', help="Failure output file (all)")
def logs2csv(dirname, log_file, csv_file, success, failure):
    """Read a local log directory and log file and output a CSV"""
    print('Reading directory...')
    mydirinfo = pulldirinfo(Path(dirname))
    if log_file:
        print('Reading log file...')
        addlogdetails(mydirinfo, Path(log_file))
    if csv_file:
        print('Writing CSV file...')
        with open(csv_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=mydirinfo[0].keys())
            writer.writeheader()
            for myitem in mydirinfo:
                writer.writerow(myitem)
    if success or failure:
        print('Analyzing...')
        mydirinfo.sort(key=lambda x: x['updated'])
        simple_list = {}
        for myitem in mydirinfo:
            if myitem['ignoreerrors']:
                if (myitem['backend'][0], myitem['url']) in simple_list:
                    continue
                simple_list[(myitem['backend'][0], myitem['url'])] = (myitem['type'], False)
                continue
            simple_list[(myitem['backend'][0], myitem['url'])] = (myitem['type'], myitem['success'])
        if success:
            with open(success, 'w') as succ_f:
                for key, val in simple_list.items():
                    if val[1] and val[0] == 'playlist':
                        print('\t'.join(key), file=succ_f)
        if failure:
            with open(failure, 'w') as fail_f:
                for key, val in simple_list.items():
                    if not val[1]:
                        print('\t'.join(key), file=fail_f)


if __name__ == '__main__':
    logs2csv()
