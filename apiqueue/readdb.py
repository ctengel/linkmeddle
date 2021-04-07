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
        possiblefiles = None
        if mediafile not in mediaf:
            trymkv = os.path.splitext(mediafile)[0] + '.mkv'
            if trymkv in mediaf:
                mediafile = trymkv
            else:
                possiblefiles = [mediafile, trymkv]
                mediafile = None
        if mediafile:
            mediaf.remove(mediafile)
        itr = {'ijf': ijf, 'ijfn': item, 'mediafile': mediafile, 'extractor_key': ijf.get('extractor_key').lower(), 'id': ijf.get('id'), 'possiblefiles': possiblefiles}
        extra_info(itr)
        output.append(itr)
    for item in mediaf + tinyf:
        if item.endswith('.jpg') or item.endswith('.webp') or item.endswith('.en.vtt'):
            continue
        itr = {'ijf': None, 'ijfn': None, 'mediafile': item, 'extractor_key': None, 'id': None}
        extra_info(itr)
        output.append(itr)
    return output

INTERESTING = ['uploader', 'playlist', 'playlist_id', 'channel_id', 'uploader_id', 'thumbnail', 'title', 'upload_date', 'webpage_url', 'uploader_url', 'channel_url']

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


def analyze(items):
    orphan = [x['mediafile'] for x in items if x['mediafile'] and (not x['in_archive'] or not x['ijfn'])]
    faildl = [x['webpage_url'] for x in items if not x['in_archive'] and x['ijfn']]
    nomedia = [x['webpage_url'] for x in items if not x['mediafile']]
    ulers = {x['channel_url'] for x in items if x['channel_url']} | {x['uploader_url'] for x in items if x['uploader_url']}
    for x in list(ulers):
        if x.startswith('http://') and x.replace('http://', 'https://') in ulers:
            ulers.remove(x)
    for x in items:
        if not x.get('channel_url'):
            x['channel_url'] = ''
        if not x.get('uploader_url'):
            x['uploader_url'] = ''
    ulers = sorted([(len([x for x in items if x['channel_url'] == y or x['uploader_url'] == y or x['channel_url'].replace('http://', 'https://') == y or x['uploader_url'].replace('http://', 'https://') == y]), y) for y in ulers])
    playlists = {(x['extractor_key'], x['playlist_id']) for x in items if x['playlist_id']}
    playlists = sorted([(len([x for x in items if x['extractor_key'] == y[0] and x['playlist_id'] == y[1]]), y[0], y[1]) for y in playlists])
    return orphan, nomedia, ulers, playlists, faildl

def stripchannels(pls, chans):
    """Remove channels from playlist list"""
    for x in list(pls):
        for y in chans:
            if x[1] in y[1].split('/')[2] and y[1].endswith(x[2]):
                pls.remove(x)

@click.command()
@click.argument('dirname')
@click.argument('arcfile')
@click.argument('outcsv')
@click.option('-s', '--skip-missing', help='Do not output missing files', is_flag=True)
@click.option('-i', '--investigate', help='files to retry', type=click.File('w'))
@click.option('-w', '--watch', help='urls to refresh', type=click.File('w'))
def logs2csv(dirname, arcfile, outcsv, skip_missing, investigate, watch):
    """Read a local ytdl directory and archive file and output a CSV"""
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
    print('analyzing')
    orphan, nomedia, ulers, playlists, faildl = analyze(myclean)
    print('orphan files (no metadata)')
    for x in orphan:
        print(x)
    if(investigate):
        investigate.write('\n'.join(faildl))
        investigate.write('\n')
        investigate.close()
    print()
    if not skip_missing:
        print('no media (just metadata)')
        for x in nomedia:
            print(x)
        print()
    stripchannels(playlists, ulers)
    print('failed')
    for x in faildl:
        print(x)
    print()
    print('uploaders')
    for x in ulers:
        print("{}\t{}".format(x[1], x[0]))
    print()
    for x in playlists:
        print("{}:{}\t{}".format(x[1], x[2], x[0]))
    print()
    if(watch):
        watch.write('\n'.join([x[1] for x in ulers]))
        if ulers:
            watch.write('\n')
        watch.write('\n'.join(['https://www.youtube.com/playlist?list={}'.format(x[2]) for x in playlists if x[1] == 'youtube']))
        if playlists:
            watch.write('\n')
        watch.close()
    print('done!')

if __name__ == '__main__':
    logs2csv()
