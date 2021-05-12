#!/usr/bin/env python3

"""Tools for deleting many files and metadata from a youtube-dl library"""


from pathlib import Path
import click


def delone(item, verbose=True):
    """Delete one file given a Path object"""
    if verbose:
        print(item, item.exists())
    if item.exists():
        item.unlink()
    if verbose:
        print(item, item.exists())

def txt2paths(textfile):
    """Read a text file and output a list of Path objects"""
    with open(textfile) as filehandle:
        paths = [Path(x.strip('\n')) for x in filehandle]
    return paths

def csv2paths(csvfile, users):
    """Return paths associated with users from a text file"""
    # TODO implement to close #55
    assert False
    return []

@click.command()
@click.option('-t', '--textfile', help='text file with files to delete')
@click.option('-c', '--csvfile', help='CSV output from readdb.py')
@click.argument('users', nargs=-1)
def delmany(textfile=None, csvfile=None, users=None):
    """Delete many files using a text file or csv with users"""
    assert bool(csvfile and users) != bool(textfile)
    assert bool(csvfile) == bool(users)
    if textfile:
        paths = txt2paths(textfile)
    else:
        paths = csv2paths(csvfile, users)
    for item in paths:
        delone(item)


if __name__ == '__main__':
    delmany()
