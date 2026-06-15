"""
Minimal script to download a URL using yt_dlp programmatically (no subprocess).
"""

from typing import Optional
import warnings
import os
import io
import argparse
import time
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError
from obj_idx import client as oic
from yt_dlp_plugins.postprocessor.objidx_upload import ObjIdxUploadPP
from . import ytdl_arch_oi


def _exclude_live(info_dict, *, incomplete: bool) -> Optional[str]:
    # Exclude live streams by checking 'is_live' key in info_dict
    is_live = info_dict.get('is_live', False)
    if is_live:
        return f"live stream excluded: {info_dict.get('id', 'unknown')}"
    return None

def _ydl(download_archive=None, cookies: io.TextIOBase | str | None = None) -> YoutubeDL:
    # TODO user, password
    # TODO extract_flat:in_playlist, simulate, skip_download
    # TODO progress_hooks, quiet
    # TODO cachedir, nooverwrites, playlistrandom, auto_subtitles
    # TODO "format": "best", "noplaylist": True, "quiet": False, "no_warnings": True,
    opts = {'writeinfojson': True,
            'download_archive': download_archive,
            #'writethumbnail': True,
            #'writesubtitles': True,
            'sleep_interval': 8,
            'max_sleep_interval': 32,
            'restrictfilenames': True,
            'match_filter': _exclude_live}
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

def get_cookies(url: str) -> str:
    """Fetch cookies for the given URL from Crustula service.

    Returns cookies as a string suitable for yt-dlp 'cookiefile' option.
    """
    crustula_url = os.getenv("CRUSTULA_URL")
    assert crustula_url, "CRUSTULA_URL must be set to use get_cookies9"
    resp = requests.get(crustula_url + 'cookies/', params={'url': url}, timeout=5)
    resp.raise_for_status()
    return resp.json()['jar']['cookies']

def init_download(url: str, *,
                  download: bool = True,
                  oibucket: str | None = None,
                  lpmlib: str | None = None,
                  use_cookies: bool = False) -> Optional[dict]:
    """Run yt-dlp for the given URL — the worker's single yt-dlp code path.

    download=False -> Stage-1 metadata-only pull (no OI required); download=True -> Stage-2
    real download with OI upload (the `ObjIdxUploadPP` sets info['oi_uuid'], which the caller
    reads back as `best_oi` — no separate OI lookup). Returns the sanitized info dict on
    success, or None on a caught YoutubeDLError (callers treat None as a failed run). No
    LinkMeddlePlaylistPP — the worker owns the metadata push (job_runner.post_result).

    URL: the URL to download
    oibucket: if provided (download only), enables ObjIdx upload postprocessor with this bucket
    lpmlib: if provided, provided to OI
    use_cookies: if True, fetch cookies from Crustula and pass to yt-dlp

    For now, env variables can control behavior:
    OBJIDX_URL=
    OBJIDX_AUTH=
    CRUSTULA_URL=
    """

    # TODO consider extractor_id and id (of playlist) instead of URL
    # TODO consider input_params for arbitrary download options

    cookies = None

    # check preconditions
    if lpmlib:
        assert oibucket, "oibucket must be set to use lpmlib"
    if use_cookies:
        assert os.getenv("CRUSTULA_URL"), "CRUSTULA_URL must be set to use cookies"
        # TODO catch exceptions
        cookiestr = get_cookies(url)
        print("got cookies:", cookiestr)
        cookies = io.StringIO(cookiestr)

    download_archive = None
    if download:
        assert os.getenv("OBJIDX_URL"), "OBJIDX_URL must be set to download"
        assert os.getenv("OBJIDX_AUTH"), "OBJIDX_AUTH must be set to download"
        download_archive = ytdl_arch_oi.ObjIdxDlArch(objidx=oic.get_obj_idx_env())

    # TODO download_archive on download mode only to prevent yt-dlp skipping playlist items we already have
    # TODO flat playlist or similar to reduce calls?
    with _ydl(download_archive=download_archive, cookies=cookies) as ydl:
        try:
            # NOTE - postprocessors may also be added by setting 'postprocessors' in the opts dict
            if download and oibucket:
                ydl.add_post_processor(ObjIdxUploadPP(oibucket=oibucket, lpmlib=lpmlib))
            info = ydl.extract_info(url, download=download)
        except YoutubeDLError as e:
            # TODO callback failure to API?
            warnings.warn(f"Error downloading {url}: {str(e)}")
            time.sleep(128)
            return None
    if cookies:
        cookies.seek(0)
        print("Final cookies:", cookies.read())
        # TODO callback success to Crustula
    print("Download completed for URL:", url)
    _print_download_result(info)
    # TODO this sleep adds per-job latency to the worker loop; tune/remove now that the
    #      server owns try_on backoff
    time.sleep(64)
    return ydl.sanitize_info(info)

def cli():
    """Command-line interface to download a URL using yt-dlp programmatically."""
    parser = argparse.ArgumentParser(description="Download a URL using yt-dlp programmatically.")
    parser.add_argument("url", help="The URL to download")
    parser.add_argument("--oibucket",
                        help="Object Index bucket for ObjIdx upload postprocessor",
                        default=None)
    parser.add_argument("--lpmlib", help="LinkMeddle library name for OI postprocessor", default=None)
    parser.add_argument("--use-cookies", action="store_true", help="Enable cookie usage (not yet implemented)")
    args = parser.parse_args()
    init_download(url=args.url,
                  download=True,
                  oibucket=args.oibucket,
                  lpmlib=args.lpmlib,
                  use_cookies=args.use_cookies)
    return 0

if __name__ == "__main__":
    SystemExit(cli())
