/* Search results: title search (?q=, #134) and OI tag search (#/tags?k=&v=).
   The global header box routes here — unless the input is a URL, which goes through the
   resolve-or-add flow instead (see app.js). */

import { apiGet } from "../api.js";
import { escapeHtml, escapeAttr, thingRow, extractorChip, urlCell } from "../util.js";

export async function renderSearch(params) {
  const q = params.get("q") || "";
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="card">
      <h2>Search <span class="muted">${escapeHtml(q)}</span></h2>
      <div id="searchBody" class="spin">Searching</div>
    </div>
    ${tagFormCard()}`;
  const el = document.getElementById("searchBody");
  if (!q) {
    el.classList.remove("spin");
    el.innerHTML = `<div class="muted">Type in the search box above — titles are matched case-insensitively.</div>`;
    return;
  }
  try {
    const things = await apiGet(`/things/?q=${encodeURIComponent(q)}&limit=200`);
    el.classList.remove("spin");
    el.innerHTML = things.map((t) => thingRow(t, {
      extras: extractorChip(t.extractor_key) + urlCell(t.url),
    })).join("") || `<div class="muted">No titles match "${escapeHtml(q)}".</div>`;
  } catch {
    el.classList.remove("spin");
    el.innerHTML = `<div class="error-box">Search failed — is the backend up?</div>`;
  }
}

export async function renderTagSearch(params) {
  const k = params.get("k") || "";
  const v = params.get("v") || "";
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="card">
      <h2>Tagged <span class="data">${escapeHtml(k)}=${escapeHtml(v)}</span></h2>
      <div id="tagBody" class="spin">Searching the object store</div>
    </div>
    ${tagFormCard(k, v)}`;
  const el = document.getElementById("tagBody");
  if (!k) {
    el.classList.remove("spin");
    el.innerHTML = `<div class="muted">Pick a tag chip on a video's Stored file card, or search below.</div>`;
    return;
  }
  try {
    const result = await apiGet(`/search/tags?key=${encodeURIComponent(k)}&value=${encodeURIComponent(v)}`);
    el.classList.remove("spin");
    const things = result.things.map((t) => thingRow(t, {
      extras: extractorChip(t.extractor_key) + urlCell(t.url),
    })).join("");
    // OI files LinkMeddle has no thing for: link straight out to their source.
    const orphans = result.unmatched.map((f) => `
      <div class="item-row">
        <span class="chip">OI only</span>
        <span class="title">${f.source_url
          ? `<a href="${escapeAttr(f.source_url)}" target="_blank">${escapeHtml(f.source_url)}</a>`
          : `<span class="data">${escapeHtml(f.file_uuid)}</span>`}</span>
      </div>`).join("");
    el.innerHTML = (things + (orphans
      ? `<h3 style="margin-top:14px;">In the object store only</h3>${orphans}` : ""))
      || `<div class="muted">Nothing tagged ${escapeHtml(k)}=${escapeHtml(v)}.</div>`;
  } catch {
    el.classList.remove("spin");
    el.innerHTML = `<div class="error-box">Tag search failed — is Object Index reachable?</div>`;
  }
}

function tagFormCard(k = "", v = "") {
  // A small key=value form; submitted via app.js delegation-free inline listener below.
  setTimeout(() => {
    document.getElementById("tagForm")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const key = document.getElementById("tagKey").value.trim();
      const value = document.getElementById("tagValue").value.trim();
      if (key) location.hash = `#/tags?k=${encodeURIComponent(key)}&v=${encodeURIComponent(value)}`;
    });
  });
  return `
    <div class="card">
      <h3>Search stored files by tag</h3>
      <form id="tagForm" class="filter-bar">
        <input type="text" id="tagKey" placeholder="key (e.g. ytdl-extractor)" value="${escapeAttr(k)}" />
        <span class="data">=</span>
        <input type="text" id="tagValue" placeholder="value" value="${escapeAttr(v)}" />
        <button type="submit">Search tags</button>
      </form>
    </div>`;
}
