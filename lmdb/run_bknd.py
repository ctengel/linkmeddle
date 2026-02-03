"""
Minimal script to download a URL using yt_dlp programmatically (no subprocess).
"""

import warnings
import os
import io
import argparse
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError
from obj_idx import client as oic
from yt_dlp_plugins.postprocessor.objidx_upload import ObjIdxUploadPP
#from yt_dlp_plugins.postprocessor.linkmeddle_playlist import LinkMeddlePlaylistPP
from . import ytdl_arch_oi
from .linkmeddle_playlist import LinkMeddlePlaylistPP
# TODO install LinkMeddlePlaylistPP properly in yt_dlp_plugins

def _ydl(ignoreerrors=False, download_archive=None, cookies: io.TextIOBase | str | None = None) -> YoutubeDL:
    # TODO user, password
    # TODO extract_flat:in_playlist, simulate, skip_download
    # TODO progress_hooks, quiet
    # TODO cachedir, nooverwrites, playlistrandom, auto_subtitles
    # TODO "format": "best", "noplaylist": True, "quiet": False, "no_warnings": True,
    opts = {'writeinfojson': True,
            'download_archive': download_archive,
            #'writethumbnail': True,
            #'writesubtitles': True,
            'sleep_interval': 4,
            'max_sleep_interval': 16,
            'ignoreerrors': ignoreerrors,
            'restrictfilenames': True}
    if cookies is not None:
        opts['cookiefile'] = cookies
    return YoutubeDL(opts)

def _print_download_result(info: dict):
    # TODO use this
    # Print a concise result (filename or id) for callers to parse
    if isinstance(info, dict):
        # For playlist returns, prefer 'requested_downloads' filename when available
        if "requested_downloads" in info and info["requested_downloads"]:
            filenames = [d.get("filename") for d in info["requested_downloads"]]
            print("downloaded_files:", filenames)
        else:
            print("id:", info.get("id"), "filename:", info.get("_filename"))
    else:
        print("result:", info)

def get_cookies9(url: str) -> str:
    """Fetch cookies for the given URL from Crustula service.

    Returns cookies as a string suitable for yt-dlp 'cookiefile' option.
    """
    crustula_url = os.getenv("CRUSTULA_URL")
    assert crustula_url, "CRUSTULA_URL must be set to use get_cookies9"
    resp = requests.get(crustula_url + 'cookies/', params={'url': url}, timeout=5)
    resp.raise_for_status()
    return resp.json()['jar']['cookies']

def init_download(url: str,
                  oibucket: str | None = None,
                  lpmlib: str | None = None,
                  schedid: int | None = None,
                  maybe_playlist: bool = True,
                  use_cookies: bool = False) -> None:
    """Initiate a yt-dlp download for the given URL.
    
    URL: the URL to download
    oibucket: if provided, enables ObjIdx upload postprocessor with this bucket
    lpmlib: if provided, provided to OI
    schedid: if provided, provided to LinkMeddle playlist postprocessor
    maybe_playlist: if True, allows playlist postprocessor to trigger; else disables it
    use_cookies: if True, enables cookie usage in yt-dlp

    Returns nothing for now; in future may return job ID or similar.

    For now. env variables can control behavior:
    OBJIDX_URL=
    OBJIDX_AUTH=
    LINKMEDDLE_PLAPI=
    CRUSTULA_URL=
    """

    # TODO consider extractor_id and id (of playlist) instead of URL
    # TODO consider input_params for arbitrary download options

    cookies = None

    # check preconditions
    if oibucket:
        assert os.getenv("OBJIDX_URL"), "OBJIDX_URL must be set to use ObjIdx upload"
        assert os.getenv("OBJIDX_AUTH"), "OBJIDX_AUTH must be set to use ObjIdx upload"
    if lpmlib:
        assert oibucket, "oibucket must be set to use lpmlib"
    if maybe_playlist:
        assert os.getenv("LINKMEDDLE_PLAPI"), "LINKMEDDLE_PLAPI must be set to use LinkMeddle playlist postprocessor"
    if schedid:
        assert maybe_playlist, "maybe_playlist must be True to use schedid"
    if use_cookies:
        assert os.getenv("CRUSTULA_URL"), "CRUSTULA_URL must be set to use cookies"
        cookiestr = get_cookies9(url)
        print("got cookies:", cookiestr)
        cookies = io.StringIO(cookiestr)

    download_archive = ytdl_arch_oi.ObjIdxDlArch(objidx=oic.get_obj_idx_env())

    with _ydl(download_archive=download_archive, cookies=cookies) as ydl:
        try:
            # NOTE - postprocessors may also be added by setting 'postprocessors' in the opts dict
            if oibucket:
                ydl.add_post_processor(ObjIdxUploadPP(oibucket=oibucket, lpmlib=lpmlib))
            if maybe_playlist:
                ydl.add_post_processor(LinkMeddlePlaylistPP(schedid=str(schedid) if schedid else None), when='playlist')
            info = ydl.extract_info(url)  #, download=True)
        except YoutubeDLError as e:
            # TODO callback failure to API?
            warnings.warn(f"Error downloading {url}: {str(e)}")
            return
    if cookies:
        cookies.seek(0)
        print("Final cookies:", cookies.read())
        # TODO callback success to Crustula
    print("Download completed for URL:", url)
    #    print(json.dumps(ydl.sanitize_info(info)))
    #retcode = json.loads(json.dumps(retcode, default=lambda o: repr(o)))
    _print_download_result(info)

def cli():
    parser = argparse.ArgumentParser(description="Download a URL using yt-dlp programmatically.")
    parser.add_argument("url", help="The URL to download")
    parser.add_argument("--oibucket",
                        help="Object Index bucket for ObjIdx upload postprocessor",
                        default=None)
    parser.add_argument("--lpmlib", help="LinkMeddle library name for playlist postprocessor", default=None)
    parser.add_argument("--schedid",
                        type=int,
                        help="Schedule ID for playlist postprocessor",
                        default=None)
    parser.add_argument("--no-playlist", action="store_true", help="Disable playlist postprocessor")
    parser.add_argument("--use-cookies", action="store_true", help="Enable cookie usage (not yet implemented)")
    init_download(url=parser.parse_args().url,
                  oibucket=parser.parse_args().oibucket,
                  lpmlib=parser.parse_args().lpmlib,
                  schedid=parser.parse_args().schedid,
                  maybe_playlist=not parser.parse_args().no_playlist,
                  use_cookies=parser.parse_args().use_cookies)
    return 0

if __name__ == "__main__":
    SystemExit(cli())
