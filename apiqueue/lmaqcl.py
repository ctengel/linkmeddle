"""Code to connect to linkmeddle API queue over REStful API"""


from posixpath import join as urljoin
import requests


class LinkMeddleClient:
    """LinkMeddle RESTful API client"""

    # TODO common get/post api with raise for status and json

    def __init__(self, api):
        self.api = api

    def import_info(self, info, info_name=None, media_name=None, localdir=None):
        """Import an info JSON into server, include in ytdl archive"""
        # TODO modernize to support multiple backends
        # TODO support only passing media filename for archive
        resp = requests.post(urljoin(self.api, 'import'),
                             json={'sourcesys': None,
                                   'sourcedir': localdir,
                                   'ijf': info,
                                   'ijfn': info_name,
                                   'mediafile': media_name})
        resp.raise_for_status()
        return resp.json().get('result')

    def all_downloads(self):
        """Return all download IDs"""
        # TODO optionally return download URLs also
        resp = requests.get(urljoin(self.api, 'download/'))
        resp.raise_for_status()
        return [x['id'] for x in resp.json()['downloads']]

    def download_detail(self, dlid):
        """Return details of a given download ID"""
        resp = requests.get(urljoin(self.api, 'download/', dlid))
        resp.raise_for_status()
        return resp.json()

    def start_download(self, url, backend=None):
        """Initiate download of given URL and return ID"""
        # TODO support more than just URL
        resp = requests.post(urljoin(self.api, 'download/'),
                             json={'url': url, 'backend': [backend, None]})
        resp.raise_for_status()
        return resp.json().get('id')
