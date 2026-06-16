"""
Minimal script to download a URL using yt_dlp programmatically (no subprocess).
"""

from typing import Optional
import datetime
import warnings
import os
import io
import json
import argparse
import time
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError
from obj_idx import client as oic
from yt_dlp_plugins.postprocessor.objidx_upload import ObjIdxUploadPP
from . import models, ytdl_arch_oi


def _exclude_live(info_dict, *, incomplete: bool) -> Optional[str]:
    # Exclude live streams by checking 'is_live' key in info_dict
    is_live = info_dict.get('is_live', False)
    if is_live:
        return f"live stream excluded: {info_dict.get('id', 'unknown')}"
    return None

def _ydl(download_archive=None, cookies: io.TextIOBase | str | None = None,
         extract_flat: bool = False) -> YoutubeDL:
    # TODO user, password
    # TODO simulate, skip_download
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
    if extract_flat:
        # Flat playlist pull: list entries (ids/urls/titles) without a per-video page fetch.
        opts['extract_flat'] = 'in_playlist'
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

# --- yt-dlp info dict -> thin pull contract --------------------------------------------
# The single place on the worker that touches yt-dlp's unstable shape: pull only the fields
# that land in thing/rel (xform stays free of raw yt-dlp knowledge). Replaces V3's
# PlaylistDLP.model_validate + xform.pl_dlp2lm.

def _norm_extractor(info: dict) -> Optional[str]:
    """Canonical lowercased extractor key (key, then extractor name, then flat-entry ie_key)."""
    ek = info.get("extractor_key") or info.get("extractor") or info.get("ie_key")
    return ek.lower() if ek else None


def _pull_chan(info: dict) -> models.UlChan:
    """Resolve the best uploader/channel identity from a playlist or entry dict."""
    return models.UlChan(url=info.get("uploader_url") or info.get("channel_url"),
                           native_id=info.get("uploader_id") or info.get("channel_id"),
                           title=info.get("uploader"))


def extract_pull_video(info: dict) -> models.VidFull:
    """Build a thin VidFull from a raw yt-dlp entry, carrying the raw entry as the hint."""
    ts = info.get("timestamp")
    return models.VidFull(
        # Flat entries carry `url` (not `webpage_url`); full entries carry both.
        url=info.get("webpage_url") or info.get("url"),
        native_id=info["id"],
        extractor_key=_norm_extractor(info),
        title=info.get("title"),
        thumbnail_url=info.get("thumbnail"),
        modified=datetime.datetime.fromtimestamp(ts) if ts else None,
        channel=_pull_chan(info),
        # Faithful copy of the raw entry -> Stage-2 load-info hint (needs real `formats`).
        info_json={k: v for k, v in info.items() if k != "info_json"})


def extract_pull_pl_stub(info: dict) -> Optional[models.PlaylistFull]:
    """Build a thin sub-container stub (no members) from a flat playlist-typed entry.

    A channel's flat pull lists its tabs/playlists as playlist-typed entries; each becomes a
    `container` thing pulled on its own later, so we keep only identity + display here.
    Returns None when the entry has no usable URL (PlaylistFull requires one).
    """
    url = info.get("webpage_url") or info.get("url")
    if not url:
        return None
    return models.PlaylistFull(
        url=url,
        native_id=info.get("id"),
        extractor_key=_norm_extractor(info),
        title=info.get("title"),
        playlist_count=info.get("playlist_count"),
        channel=_pull_chan(info))


def extract_pull(info: dict) -> models.PlaylistFull:
    """Extract the thin pull contract from a raw yt-dlp container info dict.

    Hierarchy-preserving: a video entry contributes a `VidFull` to `entries`; a
    playlist-typed entry (a channel's tab/sub-playlist) contributes a stub to
    `child_playlists` (pulled on its own later) instead of being flattened into videos.
    """
    assert info.get("webpage_url") is not None  # needed until we get an lmpl id
    entries: list[models.VidFull] = []
    child_playlists: list[models.PlaylistFull] = []
    for entry in info.get("entries") or []:
        if entry is None:
            continue
        if entry.get("_type") == "playlist" or entry.get("entries"):
            stub = extract_pull_pl_stub(entry)
            if stub is not None:
                child_playlists.append(stub)
            continue
        entries.append(extract_pull_video(entry))
    modified = info.get("modified_date")
    return models.PlaylistFull(
        url=info["webpage_url"],
        native_id=info.get("id"),
        extractor_key=_norm_extractor(info),
        title=info.get("title"),
        modified=datetime.datetime.strptime(modified, "%Y%m%d") if modified else None,
        playlist_count=info.get("playlist_count"),
        channel=_pull_chan(info),
        entries=entries,
        child_playlists=child_playlists)


def init_download(url: str, *,
                  download: bool = True,
                  oibucket: str | None = None,
                  lpmlib: str | None = None,
                  use_cookies: bool = False,
                  flat: bool = False,
                  info_dict: dict | None = None) -> Optional[dict]:
    """Run yt-dlp for the given URL — the worker's single yt-dlp code path.

    download=False -> Stage-1 metadata-only pull (no OI required); download=True -> Stage-2
    real download with OI upload (the `ObjIdxUploadPP` sets info['oi_uuid'], which the caller
    reads back as `best_oi` — no separate OI lookup). flat -> flatten a playlist pull
    (`extract_flat`, minimal site calls); set only for a playlist `pull`, never for a
    single-video `meta`/`download` (those need a full extract). Returns the sanitized info
    dict on success, or None on a caught YoutubeDLError (callers treat None as a failed run).
    No LinkMeddlePlaylistPP — the worker owns the metadata push (job_runner.post_result).

    URL: the URL to download
    oibucket: if provided (download only), enables ObjIdx upload postprocessor with this bucket
    lpmlib: if provided, provided to OI
    use_cookies: if True, fetch cookies from Crustula and pass to yt-dlp
    info_dict: if provided, download straight from this pre-extracted yt-dlp info dict
        (like `yt-dlp --load-info-json`) instead of re-extracting `url`. See the
        process_ie_result branch below.

    For now, env variables can control behavior:
    OBJIDX_URL=
    OBJIDX_AUTH=
    CRUSTULA_URL=
    """

    # TODO consider extractor_id and id (of playlist) instead of URL

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

    with _ydl(download_archive=download_archive, cookies=cookies, extract_flat=flat) as ydl:
        try:
            # NOTE - postprocessors may also be added by setting 'postprocessors' in the opts dict
            if download and oibucket:
                ydl.add_post_processor(ObjIdxUploadPP(oibucket=oibucket, lpmlib=lpmlib))
            if info_dict is not None:
                # Download straight from a supplied info dict, like
                # `yt-dlp --load-info-json`. This is what download_with_info_file() wraps,
                # but called directly so we keep the processed info dict (the file helper
                # returns only a retcode) — post_result still reads oi_uuid/extractor/id off it.
                info = ydl.process_ie_result(
                    ydl.sanitize_info(info_dict, ydl.params.get('clean_infojson', True)),
                    download=download)
            else:
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
    parser.add_argument("--info-json",
                        help="Download from this info.json file instead of extracting the URL "
                             "(like yt-dlp --load-info-json)",
                        default=None)
    args = parser.parse_args()
    info_dict = None
    if args.info_json:
        with open(args.info_json, encoding="utf-8") as fobj:
            info_dict = json.load(fobj)
    init_download(url=args.url,
                  download=True,
                  oibucket=args.oibucket,
                  lpmlib=args.lpmlib,
                  use_cookies=args.use_cookies,
                  info_dict=info_dict)
    return 0

if __name__ == "__main__":
    SystemExit(cli())
