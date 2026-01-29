"""
Minimal script to download a URL using yt_dlp programmatically (no subprocess).
"""

import warnings
import os
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError
from yt_dlp_plugins.postprocessor.objidx_upload import ObjIdxUploadPP
from yt_dlp_plugins.postprocessor.linkmeddle_playlist import LinkMeddlePlaylistPP

def _ydl(ignoreerrors=False, download_archive=None):
    # TODO user, password, cookiefile
    # TODO extract_flat:in_playlist, simulate, skip_download
    # TODO progress_hooks, quiet
    # TODO cachedir, nooverwrites, playlistrandom, auto_subtitles
    # TODO "format": "best", "noplaylist": True, "quiet": False, "no_warnings": True,
    opts = {'writeinfojson': True,
            'download_archive': download_archive,
            'writethumbnail': True,
            'writesubtitles': True,
            'sleep_interval': 4,
            'max_sleep_interval': 16,
            'ignoreerrors': ignoreerrors,
            'restrictfilenames': True}
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
    """

    # TODO consider extractor_id and id (of playlist) instead of URL
    # TODO consider input_params for arbitrary download options

    # TODO implement cookies
    assert not use_cookies, "Cookie support not yet implemented"

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

    # TODO download_archive support

    with _ydl() as ydl:
        try:
            # NOTE - postprocessors may also be added by setting 'postprocessors' in the opts dict
            if oibucket:
                ydl.add_post_processor(ObjIdxUploadPP(oibucket=oibucket, lpmlib=lpmlib))
            if maybe_playlist:
                ydl.add_post_processor(LinkMeddlePlaylistPP(schedid=str(schedid)), when='playlist')
            info = ydl.extract_info(url)  #, download=True)
        except YoutubeDLError as e:
            # TODO callback failure to API?
            warnings.warn(f"Error downloading {url}: {str(e)}")
            return
    print("Download completed for URL:", url)
    #    print(json.dumps(ydl.sanitize_info(info)))
    #retcode = json.loads(json.dumps(retcode, default=lambda o: repr(o)))
    _print_download_result(info)
