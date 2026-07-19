/* Pure render/format helpers + the delegated-action registry.
   All dynamic HTML is built as strings; interactive elements carry data-action/data-nav
   attributes handled by one document-level listener (no inline JS, no globals). */

export function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function escapeAttr(str) {
  if (str == null) return "";
  return String(str).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function isHttpUrl(str) {
  return /^https?:\/\//i.test(String(str || ""));
}

// The backend serializes naive UTC datetimes with no zone suffix (e.g.
// "2026-06-25T03:15:57.891485"); new Date() would parse those as *local*. Append
// "Z" when no zone is present so they're anchored to UTC before display.
export function parseUtc(s) {
  if (!s) return null;
  const hasZone = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(String(s));
  return new Date(hasZone ? s : s + "Z");
}

export function fmtDt(dt) {
  if (!dt) return "";
  return parseUtc(dt).toLocaleString(undefined, { hour12: false });
}

export function fmtSize(bytes) {
  if (bytes == null) return "";
  let n = bytes;
  for (const unit of ["B", "KiB", "MiB", "GiB", "TiB"]) {
    if (n < 1024 || unit === "TiB") return `${n < 10 && unit !== "B" ? n.toFixed(1) : Math.round(n)} ${unit}`;
    n /= 1024;
  }
}

export function isThingFailing(thing) {
  if (!thing.last_failure_dt) return false;
  if (!thing.last_success_dt) return true;
  return parseUtc(thing.last_failure_dt) > parseUtc(thing.last_success_dt);
}

/* === Chips === */

// thing.grade is computed server-side (lmfe/models.py grade_for) and present on every
// response — read it directly, never recompute the band thresholds here.
export function gradeChip(thing) {
  if (!thing.grade) return "";
  const machine = thing.human_rating == null ? " machine" : "";
  return `<span class="grade g${thing.grade}${machine}" title="${machine ? "machine rating" : "your rating"}">${thing.grade}</span>`;
}

export function typeChip(container, kind) {
  if (container === true) {
    if (kind === "channel") return `<span class="chip chip-ch">CH</span>`;
    return `<span class="chip chip-pl">PL</span>`;
  }
  if (container === false) return `<span class="chip chip-v">V</span>`;
  return `<span class="chip">?</span>`;
}

export function extractorChip(key) {
  if (!key) return "";
  return `<span class="chip">${escapeHtml(key)}</span>`;
}

export function urlCell(url) {
  if (!url) return "";
  return `<span class="url-cell">${escapeHtml(url)}</span>`;
}

// Grade-colored left rail — the row signature (band color = the app's own vocabulary).
export function railClass(thing) {
  return thing.grade ? ` rail-${thing.grade}` : "";
}

/* Row/sidebar thumbnail via the BFF's cached /things/{id}/thumb. Rendered only when the
   thing has a thumbnail_url (so no-thumb things cost no request); a failed fetch is
   hidden by the capture-phase error listener below, collapsing back to a text-only row. */
export function thumbImg(t, cls = "thumb") {
  if (!t.thumbnail_url) return "";
  return `<img class="${cls}" loading="lazy" src="/things/${t.id}/thumb" alt="" />`;
}

/* Per-list extractor filter (facet picker): "All" + a chip per facet, as links so the
   filter lives in the hash (linkable/back-button friendly). `makeHash(key)` builds the
   target hash for a key ('' = All; note facets may include null = unidentified).
   Counts are global thing-counts from /things/facets, not per-view counts. */
export function extractorChipsHTML(facets, current, makeHash) {
  const chip = (key, label, count) => `
    <a class="chip chip-filter${(current || "") === key ? " sel" : ""}"
       href="${escapeAttr(makeHash(key))}">${escapeHtml(label)}${
       count != null ? ` <span class="count">${count}</span>` : ""}</a>`;
  return `<div class="filter-chips">${chip("", "All", null)}${facets
    .filter((f) => f.extractor_key)
    .map((f) => chip(f.extractor_key, f.extractor_key, f.count)).join("")}</div>`;
}

/* One standard clickable thing row. `opts.extras` = trailing HTML chips/cells,
   `opts.actions` = HTML for right-aligned buttons, `opts.ctx` = playlist context id.
   `opts.action` makes the row fire that registered action (with data-id) instead of
   navigating (data-action wins over data-nav in the delegation); `opts.active`
   highlights the row. */
export function thingRow(t, opts = {}) {
  const ctx = opts.ctx ? `?ctx=${opts.ctx}` : "";
  const action = opts.action ? ` data-action="${opts.action}" data-id="${t.id}"` : "";
  return `
    <div class="item-row${railClass(t)}${opts.dim ? " dim" : ""}${opts.active ? " active" : ""}"
         tabindex="0" data-nav="#/thing/${t.id}${ctx}"${action}>
      ${thumbImg(t)}
      ${typeChip(t.container, t.kind)}
      <span class="title">${escapeHtml(t.title || "Untitled")}</span>
      ${gradeChip(t)}
      ${t.channel ? `<span class="muted">${escapeHtml(t.channel)}</span>` : ""}
      ${opts.extras || ""}
      ${opts.actions ? `<span class="actions">${opts.actions}</span>` : ""}
    </div>`;
}

/* === Panel fill helpers (dashboard-style cards on Viewing/Manage/Acquire) === */

export function fill(el, html, emptyMsg) {
  el.classList.remove("spin");
  el.innerHTML = html || `<div class='muted'>${emptyMsg}</div>`;
}

export function fillError(el) {
  el.classList.remove("spin");
  el.innerHTML = "<div class='error-box'>Couldn't load — is the backend up?</div>";
}

/* === Delegated actions ===
   Views register named handlers; one listener dispatches [data-action] clicks and
   [data-nav] row navigation (buttons inside rows stop propagation implicitly because
   the action handler wins). */
const actionHandlers = {};

export function registerAction(name, fn) {
  actionHandlers[name] = fn;
}

export function initActionDelegation() {
  // Image error events don't bubble — catch them in the capture phase so a thumb whose
  // fetch 404s (no upstream image) collapses the row back to its text-only form.
  document.addEventListener("error", (e) => {
    if (e.target.classList?.contains("thumb")) e.target.hidden = true;
  }, true);
  document.addEventListener("click", (e) => {
    const actionEl = e.target.closest("[data-action]");
    if (actionEl) {
      const fn = actionHandlers[actionEl.dataset.action];
      if (fn) {
        e.preventDefault();
        e.stopPropagation();
        fn(actionEl.dataset, actionEl);
      }
      return;
    }
    const navEl = e.target.closest("[data-nav]");
    if (navEl && !e.target.closest("a,button,input,select,label")) {
      location.hash = navEl.dataset.nav;
    }
  });
  // Enter on a focused row follows it (rows are tabbable for keyboard use, #134);
  // action rows fire their action, same precedence as click (native controls excluded —
  // they already handle Enter themselves).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.target.closest?.("a,button,input,select")) return;
    const actionEl = e.target.closest?.("[data-action]");
    const fn = actionEl && actionHandlers[actionEl.dataset.action];
    if (fn) {
      e.preventDefault();
      fn(actionEl.dataset, actionEl);
      return;
    }
    const navEl = e.target.closest?.("[data-nav]");
    if (navEl) location.hash = navEl.dataset.nav;
  });
}
