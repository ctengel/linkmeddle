#!/usr/bin/env python3

"""Get a blog with images"""

import pathlib
import re
import xml.etree.ElementTree as ET
import csv
import linkmeddle


def get_blog(blog_url):
    """DL entire XML"""
    xmlpath = pathlib.Path(blog_url)
    outcsv = pathlib.Path(xmlpath.stem + ".csv")
    with outcsv.open('w', newline='') as cfh:
        cdw = csv.DictWriter(cfh,fieldnames=('source', 'target'))
        cdw.writeheader()
        #outdir.mkdir()
        tree = ET.parse(xmlpath)
        root = tree.getroot()
        for post in root.findall("post"):
            slug = post.attrib['slug']
            pid = post.attrib['id']
            video = post.find('video-player')
            if video is not None:
                for link in video.text.split('"'):
                    if re.match(r"^https?\:\/\/.+\.mp4$", link):
                        cdw.writerow({"source": link,
                                      "target": "{}_{}-{}".format(slug,
                                                                  pid,
                                                                  linkmeddle.basenameurl(link))})
                        break
            else:
                print("NOVID", pid, slug)


if __name__ == "__main__":
    linkmeddle.cli(get_blog)
