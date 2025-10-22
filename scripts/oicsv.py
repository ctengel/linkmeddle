#!/usr/bin/env python3

"""Get files listed in a CSV and send to OI"""

import pathlib
import csv
import time
import warnings
import tempfile
import os
import random
import requests.exceptions
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
            # Check to see if exists first
            filez = oicl.search_files({'url': line['source']})
            if len(filez) == 1 and filez[0].info['file_object']['obj_size'] > 1024*1024:
                print(f"already got {line['source']}")
                continue
            with tempfile.NamedTemporaryFile() as temp:
                try:
                    digest, mime = oic.simple_download(line['source'], temp.name)
                except requests.exceptions.HTTPError as excp:
                    warnings.warn(str(excp))
                    continue
                if pathlib.Path(temp.name).stat().st_size < 1024*1024:
                    warnings.warn(f"{line['source']} too small!")
                    continue
                tags = {'mblr-blog-file': csvpath.stem,
                        'mblr-post-file': pathlib.PurePath(line['target']).stem}
                fileobj = oic.upload(temp.name, oicl, bucket, tags, digest, mime, line['source'])
            print(line['source'], line['target'], fileobj.uuid)
            time.sleep(random.randint(30,60))

if __name__ == "__main__":
    linkmeddle.cli(get_blog)
