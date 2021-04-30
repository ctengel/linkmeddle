#!/usr/bin/env python3

import sys

from pathlib import Path

with open(sys.argv[1]) as fh:
    paths = [Path(x.strip('\n')) for x in fh]

for item in paths:
    print(item, item.exists())
    if item.exists():
        item.unlink()
    print(item, item.exists())
