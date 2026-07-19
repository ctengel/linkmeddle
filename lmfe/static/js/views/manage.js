/* Manage: the machine's side of the story — fix it (Need to Fix, #129), Upcoming Jobs
   (#193), In Progress, Recent Activity, live streams via Pervellam when configured, and
   the (placeholder) SimplerObjects space visualizer. */

import { apiGet } from "../api.js";
import { escapeHtml, escapeAttr, thingRow, typeChip, urlCell, fmtDt,
         fill, fillError } from "../util.js";

// Cap the (otherwise unbounded) Need to Fix list — the BFF already sorts most-urgent
// first, so the tail is the least important.
const FAILED_CAP = 10;

export function renderManage() {
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="panel-grid">
      <div class="card"><h2>Need to Fix</h2><div id="needToFix" class="spin">Loading</div></div>
      <div class="card"><h2>Upcoming Jobs</h2><div id="upcomingJobs" class="spin">Loading</div></div>
      <div class="card"><h2>In Progress</h2><div id="inProgress" class="spin">Loading</div></div>
      <div class="card"><h2>Recent Activity</h2><div id="recentActivity" class="spin">Loading</div></div>
      <div class="card" id="pervellamCard" hidden><h2>Live Streams <span class="muted">pervellam</span></h2><div id="pervellam"></div></div>
      <div class="card"><h2>Space <span class="muted">SimplerObjects</span></h2>
        <div class="muted">Space usage visualizer coming soon — per-server quota and
        reclaimable D/F space will show here.</div></div>
    </div>
  `;

  loadNeedToFix();
  loadUpcomingJobs();
  loadInProgress();
  loadRecentActivity();
  loadPervellam();
}

async function loadNeedToFix() {
  const el = document.getElementById("needToFix");
  try {
    // BFF sorts: actionable (try_on set) before acked permafails, rating-desc then
    // most-recent-failure first. ?failing already excludes failures with a later success.
    const data = await apiGet("/things/?failing=true");
    const rows = data.slice(0, FAILED_CAP).map((t) => {
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
    }).join("");
    // Honest cap: name the hidden remainder rather than silently dropping broken things.
    const more = data.length > FAILED_CAP
      ? `<div class="muted">…and ${data.length - FAILED_CAP} more (see Browse)</div>`
      : "";
    fill(el, rows + more, "Nothing broken \u{1F389}");
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

// Also shown on Viewing (home.js renders a matching #recentActivity card and calls this).
export async function loadRecentActivity() {
  const el = document.getElementById("recentActivity");
  try {
    // Completed successes only: failures live in "Need to Fix", active in "In Progress".
    const data = await apiGet("/runs/?limit=20&success=true");
    fill(el, data.map(runRow).join(""), "No recent activity");
  } catch { fillError(el); }
}

// Read-only Pervellam (live-stream tool) feed; a 404 means "not configured — hide".
// Also shown on Viewing (home.js renders the matching hidden #pervellamCard).
export async function loadPervellam() {
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
  // A finished capture with a parsed OI file UUID plays in-app (#/oi/…); without one,
  // fall back to the raw OI link the fname holds.
  const pvRow = (j, done) => `
    <div class="item-row">
      <span class="chip ${done ? "chip-ok" : "chip-run"}">${escapeHtml(j.status || "?")}</span>
      <span class="title">${done && j.oi_file
        ? `<a href="#/oi/${escapeAttr(j.oi_file)}">${escapeHtml(j.url || j.fname)}</a>`
        : done && j.fname
          ? `<a href="${escapeAttr(j.fname)}" target="_blank">${escapeHtml(j.url || j.fname)}</a>`
          : escapeHtml(j.url || "")}</span>
      ${j.dler ? `<span class="chip">${escapeHtml(j.dler)}</span>` : ""}
      <span class="muted">${j.updated ? fmtDt(j.updated) : ""}</span>
    </div>`;
  const html = active.map((j) => pvRow(j, false)).join("")
    + finished.slice(0, 5).map((j) => pvRow(j, true)).join("");
  fill(el, html, "No live-stream jobs");
}
