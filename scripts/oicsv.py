#!/usr/bin/env python3

"""Get files listed in a CSV and send to OI"""

import pathlib
import csv
import time
import os
import random
from obj_idx import client as oic
import linkmeddle

BUCKET = "npr-20251018"

def get_blog(blog_url, bucket=BUCKET):
    """DL CSV"""
    # Setup OI connection
    oi_url = os.environ['OBJIDX_URL']
    oi_user = os.environ['OBJIDX_AUTH'].partition(':')[0]
    oicl = oic.get_obj_idx(oi_url, oi_user)
    # Read CSV
    csvpath = pathlib.Path(blog_url)
    with csvpath.open(newline='') as cfh:
        cdr = csv.DictReader(cfh)
        for line in cdr:
            tags = {'mblr-blog-file': csvpath.stem,
                    'mblr-post-file': pathlib.PurePath(line['target']).stem}
            # TODO consider 'target' as key_hint
            fileobj = oic.upload_remote(line['source'], oicl, bucket, extra=tags, catch_dl_err=True)
            print(line['source'], line['target'], fileobj.uuid)
            time.sleep(random.randint(30,60))

if __name__ == "__main__":
    linkmeddle.cli(get_blog)
