"""yt-dlp postprocessor plugin to POST playlist summary data to LinkMeddle API."""


import os
from typing import Any, Dict, Optional
import requests
from yt_dlp.postprocessor.common import PostProcessor, PostProcessingError
# TODO need to ensure lmdb is importable
from lmdb import models, xform


TIMEOUT = 10


class LinkMeddlePlaylistPP(PostProcessor):
    """
    After a playlist is processed by yt-dlp, POSTs summary data to /playlist-run/.
    Uses local `models` and `lmdb.xfrm` when available to shape/serialize the payload.
    """

    def __init__(self, downloader=None, schedid=None, **kwargs):
        super().__init__(downloader)
        self._kwargs = kwargs
        self.api_base = os.getenv("LINKMEDDLE_PLAPI", "http://localhost:8000")
        # ensure no trailing slash, we'll add one where needed
        self.api_base = self.api_base.rstrip("/")
        self.schedid = schedid

    def _build_payload(self, info: Dict[str, Any]) -> models.PlaylistFull:
        # Attempt to construct a structured payload.
        dlp = models.PlaylistDLP.model_validate(info)
        pl = xform.pl_dlp2lm(dlp)
        return pl

    def _post_playlist_run(self,
                           payload: models.PlaylistFull,
                           schedule_id: Optional[int] = None) -> models.PlaylistRunResult:
        url = f"{self.api_base}/playlist-run/"
        body = payload.model_dump()
        body['modified_date'] = body['modified_date'].isoformat() if body['modified_date'] else None
        for entry in body['entries']:
            entry['upload_date'] = entry['upload_date'].isoformat() if entry['upload_date'] else None
        # TODO add schedule_id to body if provided
        # TODO use PlaylistRunCreate model
        resp = requests.post(url, json={'playlist': body}, timeout=TIMEOUT)
        resp.raise_for_status()
        result = models.PlaylistRunResult.model_validate(resp.json())
        return result

    def run(self, information):
        """yt-dlp calls postprocessors with an info dict.

        We trigger when the dict looks like a playlist
        summary (contains 'entries' or _type == 'playlist').
        """
        is_playlist = information.get("_type") == "playlist" or bool(information.get("entries"))
        if is_playlist:
            self.to_screen('Attempting LM playlist-run POST...')
            try:
                # TODO do we need to sanitize information before building payload?
                # TODO send schedule id if available
                payload = self._build_payload(information)
                # Do POST but avoid blocking downloader for long: do it synchronously but quick timeout.
                result = self._post_playlist_run(payload,
                                                 schedule_id=int(self.schedid) if self.schedid else None)
            except Exception as exc:
                # never crash yt-dlp; report and continue
                raise PostProcessingError(str(exc)) from exc
            stat_id = result.new_stats.stat_id if result.new_stats else None
            res_sched_id = result.schedule.sched_id if result.schedule else None
            lm_pl_id = result.summary.playlist_id if result.summary else None
            # TODO consider using playlist count from result.summary
            lm_pl_count = len(result.summary.entries) if result.summary else None
            self.to_screen(f'LM playlist-run POST successful; LM playlist ID: {lm_pl_id} with {lm_pl_count} entries.')
            information['lm_playlist_id'] = lm_pl_id
            if stat_id and res_sched_id:
                self.to_screen(f'LM playlist-run stats collected: new stats ID: {stat_id} for schedule ID: {res_sched_id}.')
                information['lm_stats_id'] = stat_id
        else:
            self.to_screen('Not a playlist; skipping LM playlist-run POST.')
        return [], information
