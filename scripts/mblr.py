#!/usr/bin/env python3

"""Get a blog with images"""

import xml.etree.ElementTree as ET
import urllib.parse
import requests
import linkmeddle

MAXLEN = 50

def page2posts(page_xml):
    """Get an array of posts from an XML string"""
    root = ET.fromstring(page_xml)
    postselm = root.find('posts')
    return postselm.findall('post')

def get_blog(blog_url):
    """DL entire XML"""
    parsed = urllib.parse.urlparse(blog_url)
    blogname, domain = parsed.netloc.split('.')[0:2]
    api_url = urllib.parse.urlunparse((parsed.scheme,
                                       parsed.netloc,
                                       '/api/read',
                                       None,
                                       None,
                                       None))
    offset = 0
    all_posts = []
    while True:
        resp = requests.get(api_url, params={"start": offset, "num": MAXLEN})
        posts = page2posts(resp.content)
        if not posts:
            break
        all_posts += posts
        offset += MAXLEN
    new_top = ET.Element('lmposts', {'source': api_url})
    for pst in all_posts:
        new_top.append(pst)
    newtree = ET.ElementTree(element=new_top)
    ET.indent(newtree)
    newtree.write("{}-{}.xml".format(domain, blogname))

if __name__ == "__main__":
    linkmeddle.cli(get_blog)
