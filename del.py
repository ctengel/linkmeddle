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

def csv2paths(csvfile, users, directory='.'):
    """Return paths associated with users from a text file"""
    dirlist = list(Path(directory).iterdir())
    with open(csvfile, newline='') as csvfh:
        interest = [x for x in csv.DictReader(csvfh) if x['uploader'] in users]
    dellist = []
    for thisfile in interest:
        if thisfile['mediafile'] and thisfile['ijfn'] and not thisfile['possiblefiles'] and thisfile['mediafile'].startswith(thisfile['title']) and thisfile['ijfn'].startswith(thisfile['title']):
            assert thisfile['ijfn'].endswith('.info.json')
            base = thisfile['ijfn'][:-10]
            assert thisfile['mediafile'].startswith(base)
            dellist.append(thisfile['ijfn'])
            dellist.append(thisfile['mediafile'])
            for ckone in dirlist:
                if ckone.startswith(base) and ckone not in [thisfile['ijfn'], thisfile['mediafile']]:
                    warnings.warn('additional', ckone)
                    dellist.append(ckone)
            continue
        if thisfile['mediafile']:
            dellist.append(thisfile['mediafile'])
            continue
        warnings.warn('skipping', thisfile['title'])
    return dellist

@click.command()
@click.option('-t', '--textfile', help='text file with files to delete')
@click.option('-c', '--csvfile', help='CSV output from readdb.py')
@click.option('-p', '--pretend', help='Don\'t actually delete, just pretend')
@click.option('-d', '--directory', help='Directory for CSV mode')
@click.argument('users', nargs=-1)
def delmany(textfile=None, csvfile=None, pretend=False, directory='.', users=None):
    """Delete many files using a text file or csv with users"""
    assert bool(csvfile and users) != bool(textfile)
    assert bool(csvfile) == bool(users)

    if textfile:
        paths = txt2paths(textfile)
    else:
        paths = csv2paths(csvfile, users, directory)
    for item in paths:
        delone(item, pretend=pretend)


if __name__ == '__main__':
    delmany()
