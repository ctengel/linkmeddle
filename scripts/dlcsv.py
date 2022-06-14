#!/usr/bin/env python3

"""Get files listed in a CSV"""

import pathlib
import csv
import time
import warnings
import requests.exceptions
import linkmeddle


def get_blog(blog_url):
    """DL CSV"""
    csvpath = pathlib.Path(blog_url)
    with csvpath.open(newline='') as cfh:
        cdr = csv.DictReader(cfh)
        for line in cdr:
            try:
                linkmeddle.download(line['source'], line['target'])
            except requests.exceptions.HTTPError as excp:
                warnings.warn(str(excp))
            time.sleep(60)

if __name__ == "__main__":
    linkmeddle.cli(get_blog)
