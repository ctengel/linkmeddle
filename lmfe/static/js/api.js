/* BFF fetch helpers + the two client caches: thing pages and presigned playback URLs. */

const API_BASE = "";

async function request(method, path, body) {
  const res = await fetch(API_BASE + path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = new Error(res.statusText);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

export const apiGet = (path) => request("GET", path);
export const apiPost = (path, body) => request("POST", path, body);
export const apiPatch = (path, body) => request("PATCH", path, body);
export const apiPut = (path, body) => request("PUT", path, body);
export const apiDelete = (path) => request("DELETE", path);

export const thingCache = {};     // id -> ThingPage
export const playbackCache = {};  // id -> PlaybackInfo {download_url, oi_info, ...} (null if no media)

export async function getThingCached(id) {
  if (thingCache[id]) return thingCache[id];
  const t = await apiGet(`/things/${id}`);
  thingCache[id] = t;
  return t;
}

export function invalidateCache(id) {
  delete thingCache[id];
  delete playbackCache[id];
}

// Lazily resolve (and cache) a video's playback info (presigned URL + OI file details) via
// the BFF, off the page view-model — so opening a container does not presign every child's
// OI URL up front.
export async function getPlaybackInfoCached(id) {
  if (id in playbackCache) return playbackCache[id];
  try {
    playbackCache[id] = await apiGet(`/things/${id}/playback`);
  } catch {
    playbackCache[id] = null;
  }
  return playbackCache[id];
}

export async function getPlaybackCached(id) {
  return (await getPlaybackInfoCached(id))?.download_url ?? null;
}

/* Patch the rating-derived fields of `id` wherever it is cached (the thing's own cached
   page and any cached container's `related` list), so subsequent renders show the new
   rating without refetching. */
export function applyRatingUpdate(id, updated) {
  const patch = {
    human_rating: updated.human_rating,
    machine_rating: updated.machine_rating,
    effective_rating: updated.effective_rating,
    grade: updated.grade,
  };
  for (const cached of Object.values(thingCache)) {
    if (cached.id === id) Object.assign(cached, patch);
    for (const r of cached.related || []) {
      if (r.thing.id === id) Object.assign(r.thing, patch);
    }
  }
}
