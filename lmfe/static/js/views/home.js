/* Viewing (the home mode): pick something to watch — the Watch Soon entry, Browse
   shortcuts, the "Need to See" panel (machine says A — confirm it, §3.1/#129), glanceable
   Recent Activity + live-stream context (shared with Manage), and a thumbnail Library
   grid of everything downloaded. Adding/rating lives in Acquire; the rest of the
   machine-status panels live in Manage. */

import { apiGet } from "../api.js";
import { escapeHtml, thingRow, gradeChip, extractorChip, extractorChipsHTML,
         thumbImg, fill, fillError } from "../util.js";
import { loadRecentActivity, loadPervellam } from "./manage.js";

export function renderHome(params = new URLSearchParams()) {
  const extractor = params.get("extractor") || "";
  const app = document.getElementById("app");
  app.innerHTML = `
    <nav class="app-nav" style="margin-bottom:14px;">
      <a href="#/watch-soon" class="nav-watch">&#9654; Watch Soon</a>
      <a href="#/browse/channels">Channels</a>
      <a href="#/browse/tags">Tags</a>
      <a href="#/browse/extractors">Extractors</a>
    </nav>

    <div class="panel-grid" style="margin-bottom:16px;">
      <div class="card"><h2>Need to See <span class="muted">machine says A — confirm it</span></h2>
        <div id="needToSee" class="spin">Loading</div></div>
      <div class="card"><h2>Recent Activity</h2><div id="recentActivity" class="spin">Loading</div></div>
      <div class="card" id="pervellamCard" hidden><h2>Live Streams <span class="muted">pervellam</span></h2><div id="pervellam"></div></div>
    </div>

    <div class="card"><h2>Library <span class="muted">downloaded videos</span></h2>
      <div id="libraryChips"></div>
      <div id="libraryGrid" class="spin">Loading</div></div>
  `;

  loadNeedToSee();
  loadRecentActivity();
  loadPervellam();
  loadLibrary(extractor);
}

// "Wow, I need you to see this" (#129): machine-rated A, human hasn't weighed in (#218 —
// min_rating keeps machine-C stuff out of this panel; those stay in Acquire's queue).
async function loadNeedToSee() {
  const el = document.getElementById("needToSee");
  try {
    const data = await apiGet("/things/?needs_rating=true&min_rating=1.5&container=false&limit=10");
    fill(el, data.map((t) => thingRow(t, { extras: extractorChip(t.extractor_key) })).join(""),
         "Nothing new the machine is excited about");
  } catch { fillError(el); }
}

function libraryCard(t) {
  return `
    <div class="thumb-card" tabindex="0" data-nav="#/thing/${t.id}">
      <div class="thumb-card-media">${thumbImg(t, "thumb thumb-wide")}${gradeChip(t)}</div>
      <div class="thumb-card-body">
        <div class="thumb-card-title">${escapeHtml(t.title || "Untitled")}</div>
        ${t.channel ? `<div class="muted">${escapeHtml(t.channel)}</div>` : ""}
      </div>
    </div>`;
}

async function loadLibrary(extractor) {
  const el = document.getElementById("libraryGrid");
  const chipsEl = document.getElementById("libraryChips");
  try {
    const [things, facets] = await Promise.all([
      apiGet(`/things/?container=false&limit=200${extractor ? `&extractor=${encodeURIComponent(extractor)}` : ""}`),
      apiGet("/things/facets"),
    ]);
    chipsEl.innerHTML = extractorChipsHTML(facets, extractor,
      (key) => key ? `#/?extractor=${encodeURIComponent(key)}` : "#/");
    // No acquired-only server filter exists; the grid is exactly the downloaded slice.
    const owned = things.filter((t) => t.file_available)
      .sort((a, b) => (b.modified || "").localeCompare(a.modified || ""));
    el.classList.remove("spin");
    el.innerHTML = owned.length
      ? `<div class="thumb-grid">${owned.map(libraryCard).join("")}</div>`
      : `<div class="muted">Nothing downloaded${extractor ? ` for ${escapeHtml(extractor)}` : ""} yet</div>`;
  } catch { fillError(el); }
}
