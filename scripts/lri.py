#!/usr/bin/env python3

"""Download all media linked or listed on a site"""

import datetime
import json
import warnings
from urllib.parse import urlparse, parse_qs, urljoin
import linkmeddle

TITLE_EXTRA = ' - Photo Gallery'
TS_FIND = 'Added on'


def parse_highres(my_id, base_url, soup):
    """Given some soup, look for photos/zips"""
    zip_url = urljoin(base_url, soup.find_all("a", class_='content-link3')[1].get("href"))
    with open(f'{my_id}.zip.url', 'w', encoding="utf8") as url_file:
        url_file.write(zip_url)
    # TODO handle zip failure case
    linkmeddle.download(zip_url, f'{my_id}.zip')

def parse_vids(soup):
    pass

def do_vids(url):
    pass

def rli_gal(url):
    """Get info on single gallery"""
    print(url)
    my_id = int(url.rsplit('=', 1)[1])
    soup = linkmeddle.getsoup(url)
    title_full = soup.find_all("title")[0].string
    if title_full.endswith(TITLE_EXTRA):
        title = title_full[:-len(TITLE_EXTRA)]
    else:
        warnings.warn(f'URL {url} has strange title {title_full}')
        title = title_full
    try:
        timestamp_str = soup.find_all("span", class_="content-title-heading")[0].string
    except IndexError:
        warnings.warn(f'URL {url} appears unparsable')
        return
    timestamp_idx = timestamp_str.rfind(TS_FIND) + len(TS_FIND)
    timestamp = datetime.datetime.strptime(timestamp_str[timestamp_idx:].strip(), '%m/%d/%Y').date()
    type_links = soup.find_all("a", class_="navtab-link")
    description = soup.find_all("td", class_="story")[0].string
    categories = []
    models = []
    for content_link in soup.find_all("a", class_='content-link1'):
        href = content_link.get('href')
        string = content_link.string
        if href.startswith('category.php'):
            categories.append(string)
        elif href.startswith('sets.php'):
            models.append(string)
        else:
            warnings.warn(f'Cannot parse content link {string} to {href}')
    types = [parse_qs(urlparse(x.get("href")).query)['type'][0] for x in type_links]
    with open(f'{my_id}.json', 'w', encoding="utf8") as json_file:
        json.dump({'id': my_id,
                   'timestamp': str(timestamp),
                   'title': title,
                   'description': description,
                   'categories': categories,
                   'models': models,
                   'gallery_types': types},
                  json_file)
    if 'highres' in types:
        parse_highres(my_id, url, soup)
    #if 'vids' in types:
    #    parse_vids()
    #    pass

if __name__ == '__main__':
    linkmeddle.cli(rli_gal)
