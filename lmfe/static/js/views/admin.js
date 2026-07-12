/* Admin: a filterable table over /things/ with multi-select bulk actions (the "ack twenty
   permafails at once" tool). Filters live in the hash so results are linkable; bulk
   actions loop PATCH /things/{id} with a progress readout and per-row failure marks. */

import { apiGet, apiPatch, invalidateCache } from "../api.js";
import { escapeHtml, escapeAttr, fmtDt, gradeChip, typeChip } from "../util.js";

let results = []; // things currently shown, in table order

export async function renderAdmin(params) {
  const app = document.getElementById("app");
  const get = (k) => params.get(k) || "";
  app.innerHTML = `
    <div class="card">
      <h2>Admin</h2>
      <form id="adminFilters" class="filter-bar">
        <input type="text" name="q" placeholder="title contains…" value="${escapeAttr(get("q"))}" />
        <select name="container">
          <option value="">any type</option>
          <option value="true" ${get("container") === "true" ? "selected" : ""}>containers</option>
          <option value="false" ${get("container") === "false" ? "selected" : ""}>videos</option>
        </select>
        <input type="text" name="extractor" placeholder="extractor" size="10" value="${escapeAttr(get("extractor"))}" />
        <select name="min_rating">
          <option value="">any grade</option>
          <option value="1.5" ${get("min_rating") === "1.5" ? "selected" : ""}>A only</option>
          <option value="0.5" ${get("min_rating") === "0.5" ? "selected" : ""}>B and up</option>
          <option value="-0.5" ${get("min_rating") === "-0.5" ? "selected" : ""}>C and up</option>
        </select>
        <label><input type="checkbox" name="failing" ${get("failing") ? "checked" : ""}/> failing</label>
        <label><input type="checkbox" name="needs_rating" ${get("needs_rating") ? "checked" : ""}/> unrated</label>
        <label><input type="checkbox" name="due" ${get("due") ? "checked" : ""}/> due</label>
        <label><input type="checkbox" name="new" ${get("new") ? "checked" : ""}/> new</label>
        <label>limit <input type="text" name="limit" size="4" value="${escapeAttr(get("limit") || "200")}" /></label>
        <button type="submit" class="primary">Filter</button>
      </form>

      <div class="bulk-bar">
        <span class="sel-count" id="selCount">0 selected</span>
        <button data-bulk="run-today">Run Today</button>
        <button data-bulk="ack">Ack Permafail</button>
        <button data-bulk="cookies-always">Cookies: Always</button>
        <button data-bulk="cookies-auto">Cookies: Auto</button>
        <select id="bulkGrade">
          <option value="">grade…</option>
          <option value="2">A</option><option value="1">B</option><option value="0">C</option>
          <option value="-1">D</option><option value="-2">F</option>
        </select>
        <button data-bulk="grade">Set grade</button>
        <input type="text" id="bulkLib" placeholder="library tag" size="10" />
        <button data-bulk="lib">Set library</button>
        <span id="bulkProgress" class="muted"></span>
      </div>

      <div class="table-wrap" id="adminTable"><div class="spin">Loading</div></div>
    </div>
  `;

  document.getElementById("adminFilters").addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const qs = new URLSearchParams();
    for (const [k, v] of fd.entries()) {
      if (v === "" || v === null) continue;
      qs.set(k, v === "on" ? "true" : v);
    }
    location.hash = `#/admin?${qs}`;
  });

  document.querySelectorAll("[data-bulk]").forEach((btn) =>
    btn.addEventListener("click", () => runBulk(btn.dataset.bulk)));

  await loadTable(params);
}

async function loadTable(params) {
  const el = document.getElementById("adminTable");
  const qs = new URLSearchParams();
  for (const k of ["q", "container", "extractor", "min_rating", "failing", "needs_rating", "due", "new", "limit"]) {
    const v = params.get(k);
    if (v) qs.set(k, v);
  }
  if (!qs.has("limit")) qs.set("limit", "200");
  try {
    results = await apiGet(`/things/?${qs}`);
  } catch {
    el.innerHTML = `<div class="error-box">Couldn't load — is the backend up?</div>`;
    return;
  }
  if (!results.length) {
    el.innerHTML = `<div class="muted" style="padding:12px 0;">No things match these filters.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="admin">
      <thead><tr>
        <th><input type="checkbox" id="selAll" aria-label="Select all" /></th>
        <th></th><th></th><th>Title</th><th>Channel</th><th>Extractor</th>
        <th>Next run</th><th>Last success</th><th>Last failure</th><th>File</th>
      </tr></thead>
      <tbody>
        ${results.map((t) => `
          <tr data-row="${t.id}">
            <td><input type="checkbox" class="rowSel" data-id="${t.id}" aria-label="Select" /></td>
            <td>${gradeChip(t)}</td>
            <td>${typeChip(t.container, t.kind)}</td>
            <td class="t"><a href="#/thing/${t.id}">${escapeHtml(t.title || "Untitled")}</a></td>
            <td>${escapeHtml(t.channel || "")}</td>
            <td class="data">${escapeHtml(t.extractor_key || "")}</td>
            <td class="data">${t.try_on || `<span class="muted">—</span>`}</td>
            <td class="data">${t.last_success_dt ? fmtDt(t.last_success_dt) : ""}</td>
            <td class="data">${t.last_failure_dt ? fmtDt(t.last_failure_dt) : ""}</td>
            <td>${t.file_available ? "&#128190;" : ""}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  document.getElementById("selAll").addEventListener("change", (e) => {
    document.querySelectorAll(".rowSel").forEach((c) => { c.checked = e.target.checked; });
    updateSelCount();
  });
  el.addEventListener("change", (e) => {
    if (e.target.classList.contains("rowSel")) updateSelCount();
  });
}

function selectedIds() {
  return [...document.querySelectorAll(".rowSel:checked")].map((c) => c.dataset.id);
}

function updateSelCount() {
  const el = document.getElementById("selCount");
  if (el) el.textContent = `${selectedIds().length} selected`;
}

const BULK_PAYLOADS = {
  "run-today": () => ({ try_on: new Date().toISOString().split("T")[0] }),
  "ack": () => ({ try_on: null }),
  "cookies-always": () => ({ cookies: true }),
  "cookies-auto": () => ({ cookies: null }),
  "grade": () => {
    const v = document.getElementById("bulkGrade").value;
    return v === "" ? null : { human_rating: Number(v) };
  },
  "lib": () => {
    const v = document.getElementById("bulkLib").value.trim();
    return { lpm_lib: v === "" ? null : v };
  },
};

async function runBulk(action) {
  const ids = selectedIds();
  const progress = document.getElementById("bulkProgress");
  if (!ids.length) { progress.textContent = "Select some rows first."; return; }
  const payload = BULK_PAYLOADS[action]?.();
  if (!payload) { progress.textContent = "Pick a grade first."; return; }
  let done = 0, failed = 0;
  for (const id of ids) {
    progress.textContent = `${done + failed + 1}/${ids.length}…`;
    try {
      await apiPatch(`/things/${id}`, payload);
      invalidateCache(id);
      done++;
      document.querySelector(`tr[data-row="${id}"]`)?.classList.remove("bulk-fail");
    } catch {
      failed++;
      document.querySelector(`tr[data-row="${id}"]`)?.classList.add("bulk-fail");
    }
  }
  progress.textContent = `Done: ${done} updated${failed ? `, ${failed} failed (marked red)` : ""}.`;
}
