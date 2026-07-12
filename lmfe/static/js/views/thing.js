/* Thing page: player + info + graph neighbors, and the Watch Soon queue.

   Playlist navigation inside one container takes the *in-place* path: the persistent
   <video> element gets a src swap and only the info card, sidebar highlight, and
   "In Playlists" panel (#133) re-render. That keeps playback position on rating (#182)
   AND keeps Picture-in-Picture alive as autoplay advances (the player module owns the
   element). */

import { apiGet, apiPatch, getThingCached, getPlaybackInfoCached, invalidateCache,
         thingCache, applyRatingUpdate } from "../api.js";
import { escapeHtml, escapeAttr, isHttpUrl, fmtDt, fmtSize, isThingFailing,
         typeChip, gradeChip, extractorChip, thingRow, registerAction } from "../util.js";
import { queue, setQueue, clearQueue, mountVideo, prefetchUpcomingPlayback,
         togglePiP, autoPlayEnabled, setAutoPlay } from "../player.js";
import { rerender } from "../router.js";

/* === Watch Soon ===
   Sentinel id for the client-only "Watch Soon" queue. It masquerades as a container so the
   standard player (sidebar + arrow/autoplay navigation, all keyed on ctx) drives the walk;
   it lives only in thingCache for the session, so a reload degrades to a plain video (#129). */
export const WATCH_SOON_CTX = "watch-soon";

export async function renderWatchSoon() {
  const app = document.getElementById("app");
  app.innerHTML = `<div class="card"><h2>&#9654; Watch Soon</h2><div class="spin">Loading</div></div>`;
  let items;
  try {
    // The backend returns only acquired, effectively B-or-better videos (#218), already in
    // weighted-random order (#129): unrated / band-A / newly-added each double a thing's
    // chance of floating to the top.
    items = await apiGet("/things/?watch_soon=true&limit=30");
  } catch {
    app.innerHTML = `<div class="card"><h2>&#9654; Watch Soon</h2><div class="error-box">Couldn't load</div></div>`;
    return;
  }
  if (!items.length) {
    app.innerHTML = `<div class="card"><h2>&#9654; Watch Soon</h2>
      <div class="muted">No B-or-better downloaded videos to watch yet.</div></div>`;
    return;
  }
  thingCache[WATCH_SOON_CTX] = {
    id: WATCH_SOON_CTX,
    title: "Watch Soon",
    container: true,
    related: items.map((t) => ({ direction: "child", channel: false, thing: t })),
  };
  location.hash = `#/thing/${items[0].id}?ctx=${WATCH_SOON_CTX}`;
}

/* === Bare OI file page (#/oi/{file_uuid}) ===
   A stripped-down player for OI files with no LM thing behind them — finished Pervellam
   captures and tag-search orphans. Same MIME-aware media area + Stored file card as a
   thing page (shared tag chips cross-navigate to LM videos); no ratings, no queue. */
export async function renderOiFilePage(fileUuid) {
  clearQueue();
  queue.currentVideoId = null;
  document.getElementById("app").innerHTML = `
    <div class="row">
      <div class="main">
        <div class="card" style="padding:0; border:none; background:none;">
          <div class="video-area" id="videoArea"></div>
          <div class="player-bar" id="playerBar"></div>
        </div>
        <div class="card" id="currentThingInfo"><div class="spin">Loading</div></div>
      </div>
    </div>
  `;
  let playback;
  try {
    playback = await apiGet(`/oi/${fileUuid}/playback`);
  } catch {
    document.getElementById("currentThingInfo").innerHTML =
      `<div class="error-box">Couldn't load this stored file — is Object Index reachable?</div>`;
    return;
  }
  const src = playback.oi_info?.source_url;
  document.getElementById("currentThingInfo").innerHTML = `
    <h1 class="thing-title">${src && isHttpUrl(src)
      ? `<a href="${escapeAttr(src)}" target="_blank">${escapeHtml(src)}</a>`
      : escapeHtml(src || fileUuid)}</h1>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-bottom:6px;">
      <span class="chip">OI only</span>
    </div>
    ${playback.object_url ? `<div class="kv">OI record: <a class="data"
        href="${escapeAttr(playback.object_url)}" target="_blank">${escapeHtml(fileUuid)}</a></div>` : ""}
    ${oiFileHTML(playback.oi_info)}
  `;
  renderMediaArea(playback.download_url, playback.oi_info);
}

/* === Thing page === */

export async function renderThingPage(thingId, ctxId) {
  // In-place update: same container context, player already on screen.
  if (ctxId && queue.containerData && queue.containerId === ctxId
      && document.getElementById("videoArea")
      && document.getElementById("currentThingInfo")) {
    await updateVideoInPlace(thingId);
    return;
  }

  const page = await getThingCached(thingId);
  queue.currentVideoId = thingId;

  if (ctxId) {
    // A missing/failed context (e.g. the client-only Watch Soon container after a reload)
    // degrades to a plain single-video view rather than erroring.
    const containerData = await getThingCached(ctxId).catch(() => null);
    if (containerData) setQueue(ctxId, containerData); else clearQueue();
  } else {
    clearQueue();
  }

  const isVideo = page.container === false;
  const isContainer = page.container === true;
  const children = (page.related || []).filter((r) => r.direction === "child");
  const parents = (page.related || []).filter((r) => r.direction === "parent");
  const indirect = page.indirect_children || [];

  document.getElementById("app").innerHTML = `
    <div class="row">
      <div class="main">
        ${isVideo ? `<div class="card" style="padding:0; border:none; background:none;">
                       <div class="video-area" id="videoArea"></div>
                       <div class="player-bar" id="playerBar"></div>
                     </div>` : ""}
        <div class="card" id="currentThingInfo"></div>
        ${isContainer ? childrenCard(children, page.id) : ""}
        ${isContainer && indirect.length ? indirectChildrenCard(indirect) : ""}
        ${isVideo ? `<div id="parentsArea">${parentsCard(parents)}</div>` : ""}
        <div class="card"><details id="runHistory"><summary class="muted" style="cursor:pointer;">Run history</summary>
          <div id="runHistoryBody"></div></details></div>
      </div>
      <div id="sidebarArea" style="display:contents;">${sidebarHTML(thingId)}</div>
    </div>
  `;

  renderInfoCard(page);
  if (isVideo) {
    renderMediaArea(page.download_url, page.oi_info);
    prefetchUpcomingPlayback(thingId);
  }
  document.getElementById("runHistory").addEventListener("toggle", (e) => {
    if (e.target.open) loadRunHistory(page.id);
  }, { once: true });
}

/* Mount the media element by MIME (objectindex gui.py parity): video via the persistent
   player element (PiP survives), audio/image inline, anything else a download link. */
function renderMediaArea(downloadUrl, oiInfo) {
  const area = document.getElementById("videoArea");
  const bar = document.getElementById("playerBar");
  if (!area) return;
  if (!downloadUrl) {
    area.innerHTML = `<div class="placeholder">File not downloaded yet</div>`;
    if (bar) bar.innerHTML = "";
    return;
  }
  const mime = oiInfo?.mime || "video/";
  const isVideo = !mime.startsWith("audio/") && !mime.startsWith("image/");
  if (mime.startsWith("audio/")) {
    area.innerHTML = `<audio controls style="width:100%;" src="${escapeAttr(downloadUrl)}"></audio>`;
  } else if (mime.startsWith("image/")) {
    area.innerHTML = `<img style="max-width:100%; display:block;" src="${escapeAttr(downloadUrl)}" alt="" />`;
  } else {
    area.innerHTML = "";
    mountVideo(area, downloadUrl);
  }
  if (bar) {
    bar.innerHTML = `
      ${isVideo && document.pictureInPictureEnabled
        ? `<button data-action="pip" title="Picture-in-Picture (p) — keeps playing as the playlist advances">&#10064; PiP</button>` : ""}
      <label class="muted"><input type="checkbox" id="autoPlayToggleBar" ${autoPlayEnabled ? "checked" : ""}/> Auto-play next</label>
      <span class="spacer"></span>
      <a class="btn" style="text-decoration:none;" href="${escapeAttr(downloadUrl)}" download>&#8595; Download</a>
    `;
    document.getElementById("autoPlayToggleBar")?.addEventListener("change", (e) => {
      setAutoPlay(e.target.checked);
    });
  }
}

/* === Info card === */

const GRADE_VALUES = { A: 2, B: 1, C: 0, D: -1, F: -2 };

function gradeOf(rating) {
  if (rating == null) return null;
  const r = Math.round(rating);
  return Object.keys(GRADE_VALUES).find((g) => GRADE_VALUES[g] === Math.max(-2, Math.min(2, r)));
}

/* The A–F band selector: grades are the app's real rating vocabulary (§2.4), so rate in
   grades. Solid fill = your rating; dashed outline = the machine's, awaiting yours. */
function rateBandHTML(thing) {
  const humanGrade = gradeOf(thing.human_rating);
  const machineGrade = thing.human_rating == null ? gradeOf(thing.effective_rating) : null;
  const buttons = ["F", "D", "C", "B", "A"].map((g) => {
    const cls = [
      humanGrade === g ? `sel-${g}` : "",
      machineGrade === g ? `sel-${g} machine-sel` : "",
    ].filter(Boolean).join(" ");
    return `<button class="${cls}" data-action="rate" data-id="${thing.id}"
                    data-value="${GRADE_VALUES[g]}" title="${g} (${GRADE_VALUES[g] >= 0 ? "+" : ""}${GRADE_VALUES[g]})">${g}</button>`;
  }).join("");
  const note = machineGrade ? `<span class="note muted">machine — click to confirm or override</span>`
    : humanGrade ? "" : `<span class="note muted">unrated</span>`;
  return `<div class="rate-band" role="group" aria-label="Rate">${buttons}${note}</div>`;
}

function infoCardHTML(thing) {
  const isFailing = isThingFailing(thing);
  const acked = !thing.try_on; // permafail acknowledged (same convention as Need to Fix)
  const status = [];
  if (thing.try_on) status.push(`<div class="kv">Next run: <b class="data">${thing.try_on}</b></div>`);
  if (thing.last_success_dt) status.push(`<div class="kv">Last success: <b class="data">${fmtDt(thing.last_success_dt)}</b></div>`);
  if (thing.last_failure_dt) {
    const s = `Last failure: <b class="data">${fmtDt(thing.last_failure_dt)}</b>`;
    status.push(`<div class="kv">${isFailing ? `<span class="failing-badge">${s}</span>` : s}</div>`);
  }
  return `
    <h1 class="thing-title">${escapeHtml(thing.title || "Untitled")}</h1>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-bottom:6px;">
      ${typeChip(thing.container, thing.kind)}
      ${extractorChip(thing.extractor_key)}
      ${gradeChip(thing)}
    </div>
    ${thing.channel ? `<div class="kv">Channel: <b>${escapeHtml(thing.channel)}</b></div>` : ""}
    ${thing.url ? `<div class="kv">URL: ${isHttpUrl(thing.url)
      ? `<a href="${escapeAttr(thing.url)}" target="_blank">${escapeHtml(thing.url)}</a>`
      : escapeHtml(thing.url)}</div>` : ""}
    <div class="kv">Library: <b>${thing.lpm_lib ? escapeHtml(thing.lpm_lib) : "—"}</b>
      &nbsp;·&nbsp; Cookies: <b>${thing.cookies === true ? "Always" : "Auto"}</b></div>
    ${status.join("")}
    ${rateBandHTML(thing)}
    <div class="actions">
      <button data-action="run-today" data-id="${thing.id}">${acked ? "Retry" : "Run Today"}</button>
      ${isFailing ? `<button class="danger" data-action="ack" data-id="${thing.id}">Permafail Ack</button>` : ""}
      <button data-action="edit-lib" data-id="${thing.id}">Edit Library</button>
      <button data-action="cookies-toggle" data-id="${thing.id}"
              data-always="${thing.cookies === true}">Cookies: ${thing.cookies === true ? "Always" : "Auto"}</button>
    </div>
    ${oiFileHTML(thing.oi_info)}
  `;
}

/* The acquired file's OI card (objectindex gui.py parity): size/MIME/checksum, the OI
   object link, and the file's tags as clickable search chips. */
function oiFileHTML(oi) {
  if (!oi) return "";
  const tags = Object.entries(oi.extra || {})
    .filter(([, v]) => typeof v === "string" || typeof v === "number")
    .map(([k, v]) => `<span class="chip chip-tag" data-action="tag-search"
        data-k="${escapeAttr(k)}" data-v="${escapeAttr(v)}"
        title="Find everything tagged ${escapeAttr(k)}=${escapeAttr(v)}">${escapeHtml(k)}=${escapeHtml(v)}</span>`)
    .join("");
  return `
    <div style="border-top:1px solid var(--line); margin-top:12px; padding-top:10px;">
      <h3>Stored file</h3>
      <div class="kv data">
        ${oi.mime ? `${escapeHtml(oi.mime)} · ` : ""}${oi.size != null ? `${fmtSize(oi.size)} · ` : ""}${oi.checksum ? `<span title="${escapeAttr(oi.checksum)}">${escapeHtml(oi.checksum.slice(0, 16))}…</span>` : ""}
      </div>
      ${oi.object_uuid ? `<div class="kv">Object: ${oi.source_url && isHttpUrl(oi.source_url)
        ? `<a class="data" href="${escapeAttr(oi.source_url)}" target="_blank">${escapeHtml(oi.object_uuid)}</a>`
        : `<span class="data">${escapeHtml(oi.object_uuid)}</span>`}</div>` : ""}
      ${tags ? `<div class="tag-chips">${tags}</div>` : ""}
    </div>`;
}

// The view-model currently shown in #currentThingInfo — the rate action re-renders from
// this (merged with the PATCH response) so page-only fields like oi_info survive a rating.
let currentInfoShown = null;

function renderInfoCard(thing) {
  currentInfoShown = thing;
  const el = document.getElementById("currentThingInfo");
  if (el) el.innerHTML = infoCardHTML(thing);
}

/* === Neighbor cards === */

function childrenCard(children, containerId) {
  if (!children.length) return `<div class="card"><div class="muted">No items yet</div></div>`;
  const rows = children.map((r) => thingRow(r.thing, {
    ctx: r.thing.container !== true ? containerId : null,
    extras: !r.thing.file_available && r.thing.container === false
      ? `<span class="muted">(no file)</span>` : "",
  })).join("");
  return `<div class="card"><h3>Contents <span class="count">${children.length}</span></h3>${rows}</div>`;
}

function indirectChildrenCard(items) {
  const rows = items.map((ic) => thingRow(ic.thing, {
    ctx: ic.container_id,
    extras: `<span class="muted">${escapeHtml(ic.container_title || "")}</span>
             ${!ic.thing.file_available ? `<span class="muted">(no file)</span>` : ""}`,
  })).join("");
  return `<div class="card"><h3>Videos <span class="count">${items.length}</span></h3>${rows}</div>`;
}

/* "In Playlists" (#133): a row click switches the right-hand queue to that playlist
   (stays on the video); the explicit Open button goes to the full playlist page.
   (Open is a data-action button, not an anchor: the delegation resolves the closest
   [data-action], so a plain anchor inside the action row would fire the row instead.) */
function parentsCard(parents) {
  if (!parents.length) return "";
  const rows = parents.map((r) => thingRow(r.thing, {
    action: "switch-queue",
    active: r.thing.id === queue.containerId,
    extras: r.channel ? `<span class="chip chip-ch">channel</span>` : "",
    actions: `<button data-action="open-thing" data-id="${r.thing.id}"
                      title="Open the full playlist page">Open</button>`,
  })).join("");
  return `<div class="card"><h3>In Playlists</h3>${rows}</div>`;
}

/* === Sidebar (active playlist queue) === */

function sidebarVideoChildren() {
  return (queue.containerData?.related || [])
    .filter((r) => r.direction === "child" && r.thing.container === false);
}

export function sidebarRowsHTML(activeId) {
  return sidebarVideoChildren().map((r) => {
    const t = r.thing;
    return `
      <div class="item-row ${t.id === activeId ? "active" : ""}" tabindex="0"
           data-nav="#/thing/${t.id}?ctx=${queue.containerId}">
        <span class="title">${escapeHtml(t.title || "Untitled")}</span>
        ${gradeChip(t)}
        ${!t.file_available ? `<span class="muted">(no file)</span>` : ""}
      </div>`;
  }).join("");
}

function sidebarHTML(activeId) {
  if (!queue.containerData || !sidebarVideoChildren().length) return "";
  // The title links to the full playlist page (#133) — except the client-only
  // Watch Soon sentinel, which has no real thing page behind it.
  const title = escapeHtml(queue.containerData.title || "Playlist");
  return `
    <div class="card sidebar">
      <h3>${queue.containerId === WATCH_SOON_CTX ? title
        : `<a href="#/thing/${queue.containerId}" title="Open the full playlist page">${title}</a>`}</h3>
      <div id="playlistSidebar">${sidebarRowsHTML(activeId)}</div>
    </div>`;
}

/* === In-place playlist advance (persistent player) === */

async function updateVideoInPlace(newVideoId) {
  // We already hold this child's metadata in the active container's `related` list, so
  // render from that cached summary; only playback info is fetched (usually a prefetch hit).
  const rel = (queue.containerData?.related || []).find((r) => r.thing.id === newVideoId);
  const page = rel ? { ...rel.thing } : await getThingCached(newVideoId);
  queue.currentVideoId = newVideoId;

  let playback = null;
  if (page.file_available) playback = await getPlaybackInfoCached(newVideoId);
  page.oi_info = playback?.oi_info || null;
  renderMediaArea(playback?.download_url || null, page.oi_info);
  renderInfoCard(page);

  const sidebar = document.getElementById("playlistSidebar");
  if (sidebar) {
    sidebar.querySelectorAll(".item-row").forEach((el) => el.classList.remove("active"));
    const idx = queue.videoIds.indexOf(newVideoId);
    if (idx !== -1) sidebar.querySelectorAll(".item-row")[idx]?.classList.add("active");
  }

  prefetchUpcomingPlayback(newVideoId);
  // In-place transitions are deliberate navigations (autoplay-next via onended, or a sidebar
  // click) — start playback. Best-effort: a browser may block play() without a user gesture.
  document.querySelector("#videoArea video")?.play().catch(() => {});

  refreshParentsPanel(newVideoId);
}

// Rebuild "In Playlists" for the video now showing (#133): the container summary we render
// from has no `related`, so fetch the video's own page (cached across revisits). Fired
// without await — media/info render first; the panel catches up when the fetch lands.
async function refreshParentsPanel(videoId) {
  const area = document.getElementById("parentsArea");
  if (!area) return;
  const full = await getThingCached(videoId).catch(() => null);
  if (!full || queue.currentVideoId !== videoId) return;  // superseded by a later navigation
  area.innerHTML = parentsCard((full.related || []).filter((r) => r.direction === "parent"));
}

async function loadRunHistory(thingId) {
  const el = document.getElementById("runHistoryBody");
  el.innerHTML = `<div class="spin">Loading</div>`;
  try {
    const runs = await apiGet(`/things/${thingId}/runs`);
    if (!runs.length) { el.innerHTML = `<div class="muted">No runs yet</div>`; return; }
    el.innerHTML = runs.map((r) => `
      <div class="item-row">
        ${r.success === null ? `<span class="chip chip-run">Running</span>`
          : r.success ? `<span class="chip chip-ok">OK</span>` : `<span class="chip chip-fail">Failed</span>`}
        <span class="data">${fmtDt(r.starttime)}</span>
        ${r.endtime ? `<span class="data muted">&rarr; ${fmtDt(r.endtime)}</span>` : ""}
        ${r.worker ? `<span class="chip">${escapeHtml(r.worker)}</span>` : ""}
        ${r.playlist_count != null ? `<span class="muted">${r.playlist_count} items</span>` : ""}
      </div>`).join("");
  } catch { el.innerHTML = `<div class="error-box">Couldn't load runs</div>`; }
}

/* === Actions (shared with the dashboard's Need to Fix panel) === */

registerAction("rate", async (d) => {
  let updated;
  try {
    updated = await apiPatch(`/things/${d.id}`, { human_rating: Number(d.value) });
  } catch {
    alert("Failed to save rating.");
    return;
  }
  // Refresh caches from the PATCH response, then re-render ONLY the rating-affected DOM:
  // routing would rebuild the <video> element and reset playback (#182) / close PiP.
  applyRatingUpdate(d.id, updated);
  const infoEl = document.getElementById("currentThingInfo");
  if (infoEl && currentInfoShown?.id === d.id) {
    renderInfoCard({ ...currentInfoShown, ...updated });
  } else if (infoEl) {
    rerender();
  }
  const sidebar = document.getElementById("playlistSidebar");
  if (sidebar) sidebar.innerHTML = sidebarRowsHTML(queue.currentVideoId);
});

registerAction("run-today", async (d) => {
  const today = new Date().toISOString().split("T")[0];
  try {
    await apiPatch(`/things/${d.id}`, { try_on: today });
    invalidateCache(d.id);
    rerender();
  } catch { alert("Failed to reschedule."); }
});

registerAction("ack", async (d) => {
  // Explicitly send try_on: null so the BFF includes it in the exclude_unset payload.
  try {
    await apiPatch(`/things/${d.id}`, { try_on: null });
    invalidateCache(d.id);
    rerender();
  } catch { alert("Failed to acknowledge."); }
});

registerAction("edit-lib", async (d) => {
  // Read the current value from cache rather than passing free text through data attrs.
  const page = await getThingCached(d.id);
  const v = prompt("LPM library tag (blank to clear):", page.lpm_lib || "");
  if (v === null) return;                          // cancelled
  const value = v.trim() === "" ? null : v.trim(); // blank clears (PATCH lpm_lib: null)
  try {
    await apiPatch(`/things/${d.id}`, { lpm_lib: value });
    invalidateCache(d.id);
    rerender();
  } catch { alert("Failed to set library."); }
});

registerAction("cookies-toggle", async (d) => {
  // Two-state UI over a tri-value backend field: "Always" (attrs.cookies === true) vs.
  // "Auto" (unset, which auto-escalates after a cookieless failure). Never write false.
  try {
    await apiPatch(`/things/${d.id}`, { cookies: d.always === "true" ? null : true });
    invalidateCache(d.id);
    rerender();
  } catch { alert("Failed to update cookies setting."); }
});

registerAction("pip", () => togglePiP());

registerAction("open-thing", (d) => { location.hash = `#/thing/${d.id}`; });

// "In Playlists" row click (#133): stay on the video, re-point the right-hand queue
// (sidebar + arrows/autoplay) at the chosen playlist.
registerAction("switch-queue", async (d) => {
  const containerData = await getThingCached(d.id).catch(() => null);
  if (!containerData || !queue.currentVideoId) return;
  setQueue(d.id, containerData);
  const area = document.getElementById("sidebarArea");
  if (area) area.innerHTML = sidebarHTML(queue.currentVideoId);
  prefetchUpcomingPlayback(queue.currentVideoId);
  refreshParentsPanel(queue.currentVideoId);  // move the active-playlist highlight
  // Update the URL without a router dispatch: replaceState doesn't fire hashchange,
  // so the persistent <video> (and PiP) keeps playing (#182).
  history.replaceState(null, "", `#/thing/${queue.currentVideoId}?ctx=${d.id}`);
});

registerAction("tag-search", (d) => {
  location.hash = `#/tags?k=${encodeURIComponent(d.k)}&v=${encodeURIComponent(d.v)}`;
});
