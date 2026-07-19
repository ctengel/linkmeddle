/* The persistent player + active playlist context.

   There is exactly ONE <video> element for the whole app lifetime. Playlist navigation
   (arrows, sidebar clicks, autoplay-next) swaps `src` on that same node instead of
   rebuilding it — which is what lets Picture-in-Picture survive as the queue advances:
   Chrome keeps the PiP window bound to the element, and a src swap continues playing
   there, while destroying/recreating the element would close it. */

import { getPlaybackCached } from "./api.js";

export const queue = {
  containerId: null,    // UUID of the sidebar/autoplay container (or the watch-soon sentinel)
  containerData: null,  // ThingPage of the active container
  videoIds: [],         // ordered UUIDs of video children in the active container
  currentVideoId: null, // UUID of the currently shown video
};

export let autoPlayEnabled = true;
export function setAutoPlay(v) { autoPlayEnabled = v; }

let videoEl = null;

export function getVideoElement() {
  if (!videoEl) {
    videoEl = document.createElement("video");
    videoEl.controls = true;
    videoEl.addEventListener("ended", () => {
      if (autoPlayEnabled) navigatePlaylist(1);
    });
  }
  return videoEl;
}

/* Mount the persistent element into `container` showing `url` (with an optional poster
   thumbnail shown before playback). Re-parenting only when needed; the src swap is guarded
   so re-rendering the same video doesn't reset playback (the poster only changes alongside
   a real src swap, so it can't disturb an in-flight video either). */
export function mountVideo(container, url, poster) {
  const v = getVideoElement();
  if (v.dataset.src !== url) {
    v.dataset.src = url;
    v.src = url;
    v.poster = poster || "";
  }
  if (v.parentElement !== container) container.appendChild(v);
  return v;
}

export function setQueue(containerId, containerData) {
  queue.containerId = containerId;
  queue.containerData = containerData;
  queue.videoIds = (containerData?.related || [])
    .filter((r) => r.direction === "child" && r.thing.container === false)
    .map((r) => r.thing.id);
}

export function clearQueue() {
  queue.containerId = null;
  queue.containerData = null;
  queue.videoIds = [];
}

// True iff `id` is a child of the active container that has a downloaded file.
// Sequential navigation (arrows + autoplay) jumps over videos we don't have (#184).
export function hasFile(id) {
  const rel = (queue.containerData?.related || []).find((r) => r.thing.id === id);
  return !!rel?.thing.file_available;
}

export function navigatePlaylist(direction) {
  if (!queue.videoIds.length || !queue.currentVideoId) return;
  const idx = queue.videoIds.indexOf(queue.currentVideoId);
  if (idx === -1) return;
  for (let next = idx + direction; next >= 0 && next < queue.videoIds.length; next += direction) {
    if (hasFile(queue.videoIds[next])) {
      location.hash = `#/thing/${queue.videoIds[next]}?ctx=${queue.containerId}`;
      return;
    }
  }
}

// Warm the playback cache for the next 1-2 *acquired* videos after `id` in the active
// playlist (navigation skips file-less ones, so those are never played from here).
export function prefetchUpcomingPlayback(id) {
  const idx = queue.videoIds.indexOf(id);
  if (idx === -1) return;
  for (const nid of queue.videoIds.slice(idx + 1).filter(hasFile).slice(0, 2)) {
    getPlaybackCached(nid); // fire-and-forget
  }
}

export async function togglePiP() {
  const v = videoEl;
  if (!v || !document.pictureInPictureEnabled) return;
  try {
    if (document.pictureInPictureElement) {
      await document.exitPictureInPicture();
    } else if (v.isConnected && v.dataset.src) {
      await v.requestPictureInPicture();
    }
  } catch { /* PiP refusals (no gesture, unsupported) are non-fatal */ }
}
