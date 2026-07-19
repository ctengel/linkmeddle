/* Acquire: bring things in and decide what they're worth — the Add form (single/batch +
   bookmarklet) and the rating queue (§3.1/#129 "rate it"), with inline A–F bands so a
   whole discovery batch can be graded without leaving the page. */

import { apiGet, apiPost } from "../api.js";
import { escapeAttr, thingRow, extractorChip, urlCell, fill, fillError } from "../util.js";
import { rateBandHTML } from "./thing.js";

export function renderAcquire(prefillUrl = "") {
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

    <div class="card">
      <h2>Rating Queue <span class="muted">newly discovered &amp; awaiting a decision</span></h2>
      <div id="ratingQueue" class="spin">Loading</div>
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

  loadRatingQueue();
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

// The full needs-rating list, machine-A things included (Viewing's Need to See panel
// still headlines that slice, but this queue is where the rating actually happens).
// Each row carries an inline A–F band; the global "rate" action rerenders this view.
async function loadRatingQueue() {
  const el = document.getElementById("ratingQueue");
  try {
    const data = await apiGet("/things/?needs_rating=true&limit=50");
    fill(el, data.map((t) => thingRow(t, {
      extras: extractorChip(t.extractor_key) + urlCell(t.url),
      actions: rateBandHTML(t),
    })).join(""), "All things rated");
  } catch { fillError(el); }
}
