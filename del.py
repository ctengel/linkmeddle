#!/usr/bin/env python3

"""Tools for deleting many files and metadata from a youtube-dl library"""


from pathlib import Path
import csv
import warnings
import click


def delone(item, verbose=True, pretend=False):
    """Delete one file given a Path object"""
    if verbose:
        print(item, item.exists())
    if item.exists() and not pretend:
        item.unlink()
    if verbose:
        print(item, item.exists())

def txt2paths(textfile):
    """Read a text file and output a list of Path objects"""
    with open(textfile) as filehandle:
        paths = [Path(x.strip('\n')) for x in filehandle]
    return paths

def csv2paths(csvfile, users, directory='.', searchparam='uploader', backend='youtube'):
    """Return paths associated with users from a text file"""
    mydir = Path(directory)
    dirlist = list(mydir.iterdir())
    with open(csvfile, newline='') as csvfh:
        interest = [x for x in csv.DictReader(csvfh)
                    if x[searchparam] in users and x['extractor_key'] == backend]
    dellist = []
    for thisfile in interest:
        if thisfile['mediafile'] and thisfile['ijfn'] and not thisfile['possiblefiles'] and thisfile['mediafile'].startswith(thisfile['title']) and thisfile['ijfn'].startswith(thisfile['title']):
            assert thisfile['ijfn'].endswith('.info.json')
            base = thisfile['ijfn'][:-10]
            assert thisfile['mediafile'].startswith(base)
            dellist.append(mydir.joinpath(thisfile['ijfn']))
            dellist.append(mydir.joinpath(thisfile['mediafile']))
            for ckone in dirlist:
                if ckone.name.startswith(base) and ckone.name not in [thisfile['ijfn'],
                                                                      thisfile['mediafile']]:
                    warnings.warn('additional {}'.format(ckone))
                    dellist.append(ckone)
            continue
        if thisfile['mediafile']:
            dellist.append(mydir.joinpath(thisfile['mediafile']))
            continue
        warnings.warn('skipping {}'.format(thisfile['title']))
    return dellist

@click.command()
@click.option('-t', '--textfile', help='text file with files to delete')
@click.option('-c', '--csvfile', help='CSV output from readdb.py')
@click.option('-p', '--pretend', help='Don\'t actually delete, just pretend', is_flag=True)
@click.option('-d', '--directory', help='Directory for CSV mode')
@click.option('-b', '--backend', help='YTDL extractor to search/delete on', default='youtube')
@click.option('--playlists/--users', '-p/-u', default=False, help='Users or playlists')
@click.argument('userspls', nargs=-1)
def delmany(textfile=None,
            csvfile=None,
            pretend=False,
            directory='.',
            backend='youtube',
            playlists=False,
            userspls=None):
    """Delete many files using a text file or csv with users"""
    assert bool(csvfile and userspls) != bool(textfile)
    assert bool(csvfile) == bool(userspls)

    if textfile:
        paths = txt2paths(textfile)
    else:
        if playlists:
            searchparam='playlist'
        else:
            searchparam='uploader'
        paths = csv2paths(csvfile, userspls, directory, backend=backend, searchparam=searchparam)
    for item in paths:
        delone(item, pretend=pretend)


if __name__ == '__main__':
    delmany()
