#!/usr/bin/env python3

"""Download all media linked or listed on a wordpress site"""

import re
import json
import warnings
import linkmeddle




def download(url, target=None, cookies=None, fhead=False, referer=None, autoname=False, auth=None, ignore_nf=False):
    pass

def wp_vid(url):
    print(url)
    soup = linkmeddle.getsoup(url)
    preview_url = None
    for script in soup.findAll('script'):
        if not script.string:
            continue
        match = re.search(r"type: 'video/mp4', file: '([^']+)'", script.string)
        if not match:
            continue
        preview_url = match.group(1)
        break
    base_name = url.rsplit('/', 2)[1]
    local_file = "{}.mp4".format(base_name)
    metadata = {'title':       soup.findAll('title')[0].string,
                'description': soup.findAll('p')[0].string,
                'tags':        [x.get("href") for x in soup.findAll('a', rel='tag')],
                'date':        soup.findAll('a', class_='month pull-left')[0].get('title'),
                'preview_url': preview_url,
                'local_file':  local_file}
    json_file_name = "{}.json".format(base_name)
    with open(json_file_name, 'w') as json_file:
        json.dump(metadata, json_file)
    if not preview_url:
        warnings.warn('No preview for {}'.format(url))
        return
    linkmeddle.download(preview_url, local_file)

def wp_idx(url):
    print(url)
    soup = linkmeddle.getsoup(url)
    links = soup.find_all("a")
    for href in set([x.get("href") for x in links]):
        if '/videoentry/' not in href:
            continue
        wp_vid(href)

if __name__ == '__main__':
    linkmeddle.cli(wp_idx)
