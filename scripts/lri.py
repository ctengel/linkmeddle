#!/usr/bin/env python3

"""Download all media linked or listed on a site"""

import datetime
import json
import warnings
from urllib.parse import urlparse, parse_qs, urljoin
import linkmeddle

TITLE_EXTRA = ' - Photo Gallery'
TS_FIND = 'Added on'

def find_next_link(soup):
    """Find the next link, if there is one"""
    for next_link in soup.find_all("a", class_="pagenav-link1"):
        if next_link.string == "Next":
            return next_link.get("href")
    return None


def parse_photos(my_id, url):
    """Parse js photo album"""
    for script in linkmeddle.getsoup(url).find_all("script"):
        for line in script.string.splitlines():
            if not line.startswith('"'):
                continue
            linkmeddle.download(urljoin(url, line.split('"')[1]), directory=str(my_id))


def parse_highres(my_id, base_url, soup):
    """Given some soup, look for photos/zips"""
    zip_url = urljoin(base_url, soup.find_all("a", class_='content-link3')[1].get("href"))
    with open(f'{my_id}.zip.url', 'w', encoding="utf8") as url_file:
        url_file.write(zip_url)
    try:
        linkmeddle.download(zip_url, f'{my_id}.zip')
    except linkmeddle.requests.exceptions.HTTPError:
        for link in soup.find_all("a"):
            href = link.get("href")
            if "image.php" in href:
                parse_photos(my_id, urljoin(base_url, href))
                return


def parse_vids(my_id, url):
    """Parse videos from a link

    Recurses for multiple pages
    """
    print(url)
    soup = linkmeddle.getsoup(url)
    for vid_link in reversed(soup.find_all("a", class_='vid-link')):
        href = vid_link.get('href')
        if href.endswith('.mpg') or href.endswith('.wmv'):
            linkmeddle.download(urljoin(url, href), directory=str(my_id))
    next_link = find_next_link(soup)
    if next_link:
        parse_vids(my_id, urljoin(url, next_link))

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
    if 'vids' in types:
        parse_vids(my_id, f'{url}&type=vids')

if __name__ == '__main__':
    linkmeddle.cli(rli_gal)
