/* Dashboard: the three §3.1/#129 human cases as panels — rate it (Need to See / Needs
   Rating), fix it (Need to Fix), plus the machine's side of the story (Upcoming Jobs #193,
   In Progress, Recent Activity, and live streams via Pervellam when configured). */

import { apiGet, apiPost } from "../api.js";
import { escapeHtml, escapeAttr, thingRow, typeChip, gradeChip, extractorChip,
         urlCell, fmtDt, railClass } from "../util.js";

export async function renderHome(prefillUrl = "") {
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="card">
      <h2>Add</h2>
      <form id="addForm">
        <label><input type="checkbox" id="batchToggle"> Batch mode (one URL per line)</label>
        <div id="addInputWrap" style="margin:8px 0;">
          <input type="url" id="addInput" style="width:100%;" placeholder="Paste a playlist, channel, or video URL"
                 value="${escapeAttr(prefillUrl)}" />
        </div>
        <button type="submit" class="primary">Add / Open</button>
        <a id="bookmarkletLink" class="btn" style="text-decoration:none; margin-left:8px;" href="#">&#128204; Add to LinkMeddle</a>
        <span class="muted">&larr; drag to your bookmarks bar</span>
      </form>
      <div id="batchProgress"></div>
    </div>

    <div class="panel-grid">
      <div class="card"><h2>Need to See <span class="muted">machine says A — confirm it</span></h2><div id="needToSee" class="spin">Loading</div></div>
      <div class="card"><h2>Needs Rating</h2><div id="needsRating" class="spin">Loading</div></div>
      <div class="card"><h2>Need to Fix</h2><div id="needToFix" class="spin">Loading</div></div>
      <div class="card"><h2>Upcoming Jobs</h2><div id="upcomingJobs" class="spin">Loading</div></div>
      <div class="card"><h2>In Progress</h2><div id="inProgress" class="spin">Loading</div></div>
      <div class="card"><h2>Recent Activity</h2><div id="recentActivity" class="spin">Loading</div></div>
      <div class="card" id="pervellamCard" hidden><h2>Live Streams <span class="muted">pervellam</span></h2><div id="pervellam"></div></div>
    </div>
  `;

  const baseURL = window.location.origin + window.location.pathname;
  document.getElementById("bookmarkletLink").href =
    "javascript:(function(){var t='" + baseURL + "';var c=encodeURIComponent(window.location.href);window.location.href=t+'#/prefill/'+c;})();";

  document.getElementById("batchToggle").addEventListener("change", (e) => {
    const wrap = document.getElementById("addInputWrap");
    wrap.innerHTML = e.target.checked
      ? `<textarea id="addInput" rows="6" style="width:100%;" placeholder="One URL per line"></textarea>`
      : `<input type="url" id="addInput" style="width:100%;" placeholder="Paste a playlist, channel, or video URL" />`;
  });
  document.getElementById("addForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("addInput").value.trim();
    if (!input) return;
    if (document.getElementById("batchToggle").checked) {
      await handleBatch(input.split("\n").map((u) => u.trim()).filter(Boolean));
    } else {
      await openOrAddUrl(input);
    }
  });

  loadNeedToSee();
  loadNeedsRating();
  loadNeedToFix();
  loadUpcomingJobs();
  loadInProgress();
  loadRecentActivity();
  loadPervellam();
}

/* Resolve a pasted URL to its thing, adding it first if unknown. */
export async function openOrAddUrl(url) {
  try {
    const result = await apiGet(`/url?u=${encodeURIComponent(url)}`);
    location.hash = `#/thing/${result.id}`;
  } catch {
    try {
      const result = await apiPost("/things/", { url });
      if (result.id) location.hash = `#/thing/${result.id}`;
    } catch {
      alert("Failed to add URL.");
    }
  }
}

async function handleBatch(urls) {
  const div = document.getElementById("batchProgress");
  div.innerHTML = "";
  let ok = 0, failed = 0;
  for (const url of urls) {
    const row = document.createElement("div");
    row.className = "muted";
    row.textContent = `Adding: ${url}`;
    div.appendChild(row);
    try {
      await apiPost("/things/", { url });
      row.textContent = `✅ Added: ${url}`;
      ok++;
    } catch {
      row.textContent = `❌ Failed: ${url}`;
      failed++;
    }
  }
  const summary = document.createElement("div");
  summary.innerHTML = `<b>Done.</b> Added: ${ok} / Failed: ${failed}`;
  div.appendChild(summary);
}

function fill(el, html, emptyMsg) {
  el.classList.remove("spin");
  el.innerHTML = html || `<div class='muted'>${emptyMsg}</div>`;
}

function fillError(el) {
  el.classList.remove("spin");
  el.innerHTML = "<div class='error-box'>Couldn't load — is the backend up?</div>";
}

// "Wow, I need you to see this" (#129): machine-rated A, human hasn't weighed in (#218 —
// min_rating keeps machine-C stuff out of this panel; those stay in Needs Rating).
async function loadNeedToSee() {
  const el = document.getElementById("needToSee");
  try {
    const data = await apiGet("/things/?needs_rating=true&min_rating=1.5&container=false&limit=10");
    fill(el, data.map((t) => thingRow(t, { extras: extractorChip(t.extractor_key) })).join(""),
         "Nothing new the machine is excited about");
  } catch { fillError(el); }
}

async function loadNeedsRating() {
  const el = document.getElementById("needsRating");
  try {
    const all = await apiGet("/things/?needs_rating=true&limit=25");
    // The machine-A slice already headlines Need to See; don't show it twice.
    const data = all.filter((t) => t.grade !== "A").slice(0, 10);
    fill(el, data.map((t) => thingRow(t, { extras: extractorChip(t.extractor_key) + urlCell(t.url) })).join(""),
         "All things rated");
  } catch { fillError(el); }
}

async function loadNeedToFix() {
  const el = document.getElementById("needToFix");
  try {
    // BFF sorts: actionable (try_on set) before acked permafails, rating-desc then
    // most-recent-failure first. ?failing already excludes failures with a later success.
    const data = await apiGet("/things/?failing=true");
    fill(el, data.map((t) => {
      const acked = !t.try_on; // permafail acknowledged: keep visible but dim + labeled
      const actions = acked
        ? `<button data-action="run-today" data-id="${t.id}">Retry</button>`
        : `<button data-action="run-today" data-id="${t.id}">Run Today</button>
           <button data-action="ack" data-id="${t.id}">Ack</button>`;
      return thingRow(t, {
        dim: acked,
        extras: `${acked ? `<span class="chip">Acked</span>` : ""}
                 <span class="muted">${t.last_failure_dt ? fmtDt(t.last_failure_dt) : ""}</span>`,
        actions,
      });
    }).join(""), "Nothing broken \u{1F389}");
  } catch { fillError(el); }
}

// What the worker will pick next, in dispatch order (#193). Advisory snapshot.
async function loadUpcomingJobs() {
  const el = document.getElementById("upcomingJobs");
  try {
    const data = await apiGet("/jobs/upcoming?limit=15");
    fill(el, data.map((j) => thingRow(j.thing, {
      extras: `${j.download ? `<span class="chip chip-dl">DL</span>` : `<span class="chip">${j.kind}</span>`}
               <span class="data muted">${j.thing.try_on || ""}</span>`,
    })).join(""), "Queue is empty — nothing due");
  } catch { fillError(el); }
}

function runRow(r) {
  const statusChip = r.success === null
    ? `<span class="chip chip-run">Running</span>`
    : r.success
      ? `<span class="chip chip-ok">OK</span>`
      : `<span class="chip chip-fail">Failed</span>`;
  return `
    <div class="item-row" tabindex="0" data-nav="#/thing/${r.thing_id}">
      ${typeChip(r.container)}
      <span class="title">${escapeHtml(r.thing_title || "Untitled")}</span>
      ${statusChip}
      <span class="muted">${r.starttime ? fmtDt(r.starttime) : ""}</span>
      ${urlCell(r.thing_url)}
    </div>`;
}

async function loadInProgress() {
  const el = document.getElementById("inProgress");
  try {
    const data = await apiGet("/runs/?in_progress=true");
    fill(el, data.map(runRow).join(""), "No active jobs");
  } catch { fillError(el); }
}

async function loadRecentActivity() {
  const el = document.getElementById("recentActivity");
  try {
    // Completed successes only: failures live in "Need to Fix", active in "In Progress".
    const data = await apiGet("/runs/?limit=20&success=true");
    fill(el, data.map(runRow).join(""), "No recent activity");
  } catch { fillError(el); }
}

// Read-only Pervellam (live-stream tool) feed; a 404 means "not configured — hide".
async function loadPervellam() {
  const card = document.getElementById("pervellamCard");
  const el = document.getElementById("pervellam");
  let active, finished;
  try {
    [active, finished] = await Promise.all([
      apiGet("/pervellam/jobs?filt=active"),
      apiGet("/pervellam/jobs?filt=finished"),
    ]);
  } catch (err) {
    if (err.status !== 404) {
      card.hidden = false;
      fillError(el);
    }
    return;
  }
  card.hidden = false;
  const pvRow = (j, done) => `
    <div class="item-row">
      <span class="chip ${done ? "chip-ok" : "chip-run"}">${escapeHtml(j.status || "?")}</span>
      <span class="title">${done && j.fname
        ? `<a href="${escapeAttr(j.fname)}" target="_blank">${escapeHtml(j.url || j.fname)}</a>`
        : escapeHtml(j.url || "")}</span>
      ${j.dler ? `<span class="chip">${escapeHtml(j.dler)}</span>` : ""}
      <span class="muted">${j.updated ? fmtDt(j.updated) : ""}</span>
    </div>`;
  const html = active.map((j) => pvRow(j, false)).join("")
    + finished.slice(0, 5).map((j) => pvRow(j, true)).join("");
  fill(el, html, "No live-stream jobs");
}
