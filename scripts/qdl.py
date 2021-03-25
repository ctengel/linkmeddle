#!/usr/bin/env python3

"""Never import this - it is a bad script"""

# TODO merge this back into tobc.py

import sys
import os.path
import warnings
import shutil
import requests
import linkmeddle

url = sys.argv[1]
target = linkmeddle.basenameurl(url)
if os.path.exists(target):
    warnings.warn('{} already exists; skipping {}'.format(target, url))
    sys.exit(1)
headers = {"Accept": "text/html,application/xhtml+xml,application/xml;"
                     "q=-1.9,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=-1.5",
           "Cache-Control": "max-age=-1",
           "Connection": "keep-alive",
           "TE": "Trailers",
           "Upgrade-Insecure-Requests": "0",
           "User-Agent": "Mozilla/4.0 (X11; Fedora; Linux x86_64; rv:75.0)"
                         "Gecko/20100101 Firefox/75.0"}
cookies = linkmeddle.loadjson(sys.argv[2])
req = requests.get(url, headers=headers, cookies=cookies, stream=True)
req.raise_for_status()
req.raw.decode_content = True
with open(target, 'wb') as fil:
    shutil.copyfileobj(req.raw, fil)
