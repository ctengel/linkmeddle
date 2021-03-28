"""Look at local ytdl files"""

import os
import csv
import click
import ytdl


def read_archive(arf):
    """Get basic info from archive file"""
    ytdl.TGTAR = arf
    output = []
    for item in ytdl.read_archive():
        breakup = item.split()
        assert len(breakup) == 2 and breakup[0] and breakup[1]
        output.append({'extractor_key': breakup[0], 'id': breakup[1]})
    return output

def lsdir(tdr):
    """List directory"""
    ytdl.TGTDIR = tdr
    return ytdl.lsdir()

def infofiledata(fname):
    """Pull raw ytdl info"""
    ytdl.TGTDIR = os.path.dirname(fname)
    return ytdl.infofiledata(os.path.basename(fname))

def pulldirinfo(tdr):
    """Pull info on info files and media in a given directory"""
    infof = []
    mediaf = []
    tinyf = []
    fullist = lsdir(tdr)
    for item in fullist:
        fname = os.path.join(tdr, item)
        if item.endswith('.info.json'):
            infof.append(item)
        elif os.path.getsize(fname) >= 1024:
            mediaf.append(item)
        else:
            tinyf.append(item)
    output = []
    for item in infof:
        fname = os.path.join(tdr, item)
        ijf = infofiledata(fname)
        mediafile = ijf.get('_filename')
        if mediafile not in mediaf:
            trymkv = os.path.splitext(mediafile)[0] + '.mkv'
            if trymkv in mediaf:
                mediafile = trymkv
            else:
                mediafile = None
        if mediafile:
            mediaf.remove(mediafile)
        itr = {'ijf': ijf, 'ijfn': item, 'mediafile': mediafile, 'extractor_key': ijf.get('extractor_key').lower(), 'id': ijf.get('id')}
        extra_info(itr)
        output.append(itr)
    for item in mediaf + tinyf:
        itr = {'ijf': None, 'ijfn': None, 'mediafile': item, 'extractor_key': None, 'id': None}
        extra_info(itr)
        output.append(itr)
    return output

INTERESTING = ['uploader', 'playlist', 'playlist_id', 'channel_id', 'uploader_id', 'thumbnail', 'title', 'upload_date', 'webpage_url']

def dirar(ard, dird):
    """Combine directory and archive data"""
    for item in dird:
        item['in_archive'] = False
        for ar in ard:
            if item['extractor_key'] == ar['extractor_key'] and item['id'] == ar['id']:
                item['in_archive'] = True
                ar['found'] = True
                break
    for item in ard:
        if 'found' not in item:
            item['in_archive'] = True
            item['ijf'] = None
            item['ijfn'] = None
            item['mediafile'] = None
            extra_info(item)
            dird.append(item)
    return dird


 

def extra_info(item):  #dird):
    """Pull extra info into dir info if available"""
    ijf = item.pop('ijf', None)
    if not ijf:
        ijf = {}
    for f in INTERESTING:
        item[f] = ijf.get(f)
    #return dird
    #return item

# TODO attempt to read /logs/ directory
# TODO attempt to read celery.log
# TODO attempt to read flask.log


@click.command()
@click.argument('dirname')
@click.argument('arcfile')
@click.argument('outcsv')
def logs2csv(dirname, arcfile, outcsv):
    print('Reading directory...')
    mydirinfo = pulldirinfo(dirname)
    print('Reading archive...')
    myarcinfo = read_archive(arcfile)
    print('Combining...')
    myclean = dirar(myarcinfo, mydirinfo)
    #print('cleanup...')
    #myclean = extra_info(mycomb)
    print('writing...')
    with open(outcsv, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=myclean[0].keys())
        writer.writeheader()
        for myitem in myclean:
            writer.writerow(myitem)
    print('done!')

if __name__ == '__main__':
    logs2csv()
