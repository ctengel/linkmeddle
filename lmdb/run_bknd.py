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
import requests
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor
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
         extract_flat: bool = False, noplaylist: bool = False) -> YoutubeDL:
    # TODO user, password
    # TODO simulate, skip_download
    # TODO progress_hooks, quiet
    # TODO cachedir, nooverwrites, playlistrandom, auto_subtitles
    # TODO "format": "best", "quiet": False, "no_warnings": True,
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
    if noplaylist:
        # Single-leaf fetch: resolve a `watch?v=X&list=Y` URL to just X, never the list (#164).
        opts['noplaylist'] = True
    if cookies is not None:
        opts['cookiefile'] = cookies
    return YoutubeDL(opts)

def _print_download_result(info: dict):
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
    """Canonical lowercased extractor key, matching yt-dlp's download-archive id
    (`extractor_key` -> flat-entry `ie_key`); `extractor` IE_NAME only as a last resort."""
    ek = info.get("extractor_key") or info.get("ie_key") or info.get("extractor")
    return ek.lower() if ek else None


def _pull_chan(info: dict) -> models.UlChan:
    """Resolve the best uploader/channel identity from a playlist or entry dict.

    `channel_id` rides alongside the collapsed `native_id`: yt-dlp's uploader_id and
    channel_id are different namespaces (youtube: @handle vs UC…), and a channel tab's own
    playlist id matches channel_id, not uploader_id — self-ownership tests need both."""
    return models.UlChan(url=info.get("uploader_url") or info.get("channel_url"),
                           native_id=info.get("uploader_id") or info.get("channel_id"),
                           channel_id=info.get("channel_id"),
                           title=info.get("uploader"))


def is_container(info: dict) -> bool:
    """Does a raw yt-dlp info dict describe a container (playlist/channel) vs a single video?"""
    return info.get("_type") == "playlist" or info.get("entries") is not None


def is_both(info: dict) -> bool:
    """A result that is simultaneously a container (has entries) AND a standalone playable
    video (top-level media). yt-dlp normally returns one or the other; this ambiguous shape we
    can't classify, so the worker reports it as a failure for a human to inspect (#164)."""
    return is_container(info) and bool(
        info.get("oi_uuid") or info.get("requested_downloads") or info.get("formats"))


def result_oi_uuid(info: dict):
    """The OI upload UUID from a download result.

    yt-dlp runs post-processors on a per-format *copy* of the info dict
    (`process_video_result` stashes those copies under `requested_downloads` and
    returns the original), so `ObjIdxUploadPP`'s `oi_uuid` lands on the copy, not at
    top level. Prefer a top-level value (future-proof / load-info hint) then scan the
    per-download entries.
    """
    if info.get("oi_uuid"):
        return info["oi_uuid"]
    for dl in info.get("requested_downloads") or []:
        if dl.get("oi_uuid"):
            return dl["oi_uuid"]
    return None


def _flat_entry_container(entry: dict) -> Optional[bool]:
    """Container-ness of a flat url-result entry from its extractor's declared return type.

    yt-dlp hands back only a URL pointer for a `url`/`url_transparent` entry, so we ask the
    target extractor what it yields: `_RETURN_TYPE` 'video' -> leaf (False), 'playlist' ->
    container (True), 'any'/unknown -> NULL (the stub's own pull classifies it). This is the
    explicit yt-dlp signal that backs `InfoExtractor.is_single_video`; no heuristics.
    """
    ie_key = entry.get("ie_key")
    if not ie_key:
        return None
    try:
        rt = get_info_extractor(ie_key)._RETURN_TYPE
    except Exception:
        return None
    return {"video": False, "playlist": True}.get(rt)


def _classify_container(info: dict) -> Optional[bool]:
    """One explicit container verdict for a raw info dict (root or entry alike).

    Already playlist-shaped (`_type=='playlist'` or has `entries`) -> True; a flat url-result
    (yt-dlp handed back only a URL pointer) -> its target extractor's declared return type
    (True/False/None); otherwise a full-shape video -> False. No ie_key heuristics.
    """
    if is_container(info):
        return True
    if info.get("_type") in ("url", "url_transparent"):
        return _flat_entry_container(info)
    return False


def _node_modified(info: dict) -> Optional[datetime.datetime]:
    """Naive-UTC `modified` from a yt-dlp dict: a video's `timestamp` epoch, else a container's
    `modified_date` (YYYYMMDD).

    yt-dlp `timestamp` is a UTC epoch; store it as naive-UTC (the V4 convention,
    models.naive_utcnow) — NOT fromtimestamp()'s worker-local time. A falsy ts (None or 0 =
    epoch 1970) is intentionally NULL: no real video predates 1990, so a 0 is a placeholder,
    not a date. Do not "fix" this into 1970-01-01.

    A `modified_date` that isn't a clean YYYYMMDD is treated as absent (NULL), not an error:
    extract_node runs this on every node in the tree, so one oddly-shaped date must not abort
    the whole pull.
    """
    ts = info.get("timestamp")
    if ts:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)
    modified = info.get("modified_date")
    if not modified:
        return None
    try:
        return datetime.datetime.strptime(modified, "%Y%m%d")
    except (ValueError, TypeError):
        return None


def extract_node(info: dict) -> models.PullThing:
    """Extract the unified pull contract from any raw yt-dlp info dict (the one place that
    touches yt-dlp's unstable shape).

    Builds a single `PullThing`: identity + display fields, the `container` verdict
    (`_classify_container`), the raw dict kept verbatim as `info_json` (Stage-2 load-info hint /
    synthetic-run data), and `entries` mapped through `extract_node` for each member yt-dlp
    handed back. A flat pull lists members one level deep (the children are flat pointers with
    empty `entries`); when yt-dlp inlines a sub-playlist's own members they are carried here too
    — structural mapping of the already-fetched dict, no extra extraction.
    """
    return models.PullThing(
        # Flat entries carry `url` (not `webpage_url`); full entries/containers carry both.
        url=info.get("webpage_url") or info.get("url"),
        native_id=info.get("id"),
        extractor_key=_norm_extractor(info),
        title=info.get("title"),
        thumbnail_url=info.get("thumbnail"),
        modified=_node_modified(info),
        playlist_count=info.get("playlist_count"),
        channel=_pull_chan(info),
        container=_classify_container(info),
        # Verbatim copy of the raw dict -> Stage-2 load-info hint (needs real `formats`) and the
        # recorded data of an inlined sub-container's synthetic run; entries kept (not stripped).
        info_json=dict(info),
        entries=[extract_node(entry) for entry in info.get("entries") or [] if entry is not None])


def init_download(url: str, *,
                  download: bool = True,
                  oibucket: str | None = None,
                  lpmlib: str | None = None,
                  run_id: str | None = None,
                  thing_id: str | None = None,
                  use_cookies: bool = False,
                  flat: bool = False,
                  info_dict: dict | None = None) -> tuple[Optional[dict], bool]:
    """Run yt-dlp for the given URL — the worker's single yt-dlp code path.

    download=False -> Stage-1 metadata-only pull (no OI required); download=True -> Stage-2
    real download with OI upload (the `ObjIdxUploadPP` sets info['oi_uuid'], which the caller
    reads back as `best_oi` — no separate OI lookup); a download also sets `noplaylist` so a
    `watch?v=X&list=Y` URL resolves to the single leaf X and never the ambiguous video+playlist
    "both" shape (#164). flat -> flatten a playlist pull
    (`extract_flat`, minimal site calls); set only for a playlist `pull`, never for a
    single-video `meta`/`download` (those need a full extract). Returns
    `(info, cookies_used)`: the sanitized info dict on success (None on a caught YoutubeDLError,
    which callers treat as a failed run) and whether cookies were actually applied to this run.
    `cookies_used` is False when not requested, True when requested and fetched OK, and False
    when requested but the Crustula fetch 404'd (a cookieless fallback, #198) — the caller
    reports the real value so §4.7 escalation stays accurate.
    No LinkMeddlePlaylistPP — the worker owns the metadata push (job_runner.post_result).

    URL: the URL to download
    oibucket: if provided (download only), enables ObjIdx upload postprocessor with this bucket
    lpmlib: if provided, provided to OI
    run_id: if provided (download only), written as `lm-run-id` tag in OI object metadata
    thing_id: if provided (download only), written as `lm-thing-id` tag in OI object metadata
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
    cookies_used = False

    # check preconditions
    # Nothing to extract from: no URL to fetch and no pre-extracted info dict to load. Treat
    # it as a failed run (return None, the same as a caught YoutubeDLError below) rather than
    # handing None to ydl.extract_info, where yt-dlp's _VALID_URL regex raises an uncaught
    # TypeError ("expected string or bytes-like object, got 'NoneType'") before it can even
    # report the real reason. Hits e.g. a migrated stub whose deleted source video never got
    # its url backfilled (#147).
    if url is None and info_dict is None:
        warnings.warn("No URL and no info_dict to extract; failing run.")
        return None, cookies_used
    if lpmlib:
        assert oibucket, "oibucket must be set to use lpmlib"
    if use_cookies:
        assert os.getenv("CRUSTULA_URL"), "CRUSTULA_URL must be set to use cookies"
        try:
            cookiestr = get_cookies(url)
        except requests.exceptions.HTTPError as e:
            # Crustula has no cookies for this url (404) etc. — cookies are a soft hint (#198),
            # so fall back to a cookieless run rather than failing the whole job.
            warnings.warn(f"cookie fetch failed for {url}: {e}; continuing without cookies")
        else:
            print("got cookies:", cookiestr)
            cookies = io.StringIO(cookiestr)
            cookies_used = True

    download_archive = None
    if download:
        assert os.getenv("OBJIDX_URL"), "OBJIDX_URL must be set to download"
        assert os.getenv("OBJIDX_AUTH"), "OBJIDX_AUTH must be set to download"
        download_archive = ytdl_arch_oi.ObjIdxDlArch(objidx=oic.get_obj_idx_env())

    # A Stage-2 acquire (download=True) is always a single leaf, so set noplaylist: a
    # `watch?v=X&list=Y` URL resolves to just X (best_oi stored correctly), and the ambiguous
    # video+playlist "both" shape never arises for downloads (#164). Pulls/meta keep it off so
    # container enumeration still works; noplaylist makes the flat-on-download case moot.
    with _ydl(download_archive=download_archive, cookies=cookies, extract_flat=flat,
              noplaylist=download) as ydl:
        try:
            # NOTE - postprocessors may also be added by setting 'postprocessors' in the opts dict
            if download and oibucket:
                tags = {"lm-run-id": run_id, "lm-thing-id": thing_id}
                oitags = ",".join(f"{k}={v}" for k, v in tags.items() if v) or None
                ydl.add_post_processor(ObjIdxUploadPP(oibucket=oibucket, lpmlib=lpmlib,
                                                      oitags=oitags))
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
            return None, cookies_used
    if cookies:
        cookies.seek(0)
        print("Final cookies:", cookies.read())
        # TODO callback success to Crustula
    print("Download completed for URL:", url)
    _print_download_result(info)
    return ydl.sanitize_info(info), cookies_used

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
