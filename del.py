#!/usr/bin/env python3


from pathlib import Path
import click

@click.command()
@click.argument('textfile')
def delmany(textfile):
    """Delete many files using a text file"""
    with open(textfile) as fh:
        paths = [Path(x.strip('\n')) for x in fh]
    for item in paths:
        print(item, item.exists())
        if item.exists():
            item.unlink()
        print(item, item.exists())

if __name__ == '__main__':
    delmany()
