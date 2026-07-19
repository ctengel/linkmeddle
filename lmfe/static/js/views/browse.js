/* Browse: channels (kind=channel things), extractors (facet counts -> filtered lists),
   and the quick failing/new/due lists. Every row clicks through to the thing page. */

import { apiGet } from "../api.js";
import { escapeHtml, thingRow, urlCell, extractorChip, fmtDt } from "../util.js";

const TABS = [
  ["channels", "Channels"],
  ["extractors", "Extractors"],
  ["tags", "Tags"],
  ["failing", "Failing"],
  ["new", "New this week"],
  ["due", "Due"],
];

/* The tag explorer's vocabulary view: tags following the soft `type:value` naming
   convention group under their type (split on the first ':'); plain tags list flat.
   Every row drills into #/browse/tags?tag=<full name>. */
function tagExplorerHTML(facets) {
  if (!facets.length) return `<div class="muted">No tags yet — add one from a video's + Tag button</div>`;
  const typed = new Map();  // type -> [facet]
  const flat = [];
  for (const f of facets) {
    const i = f.name.indexOf(":");
    if (i > 0) {
      const type = f.name.slice(0, i);
      if (!typed.has(type)) typed.set(type, []);
      typed.get(type).push(f);
    } else flat.push(f);
  }
  const row = (f, label) => `
    <div class="item-row" tabindex="0" data-nav="#/browse/tags?tag=${encodeURIComponent(f.name)}">
      <span class="title">${escapeHtml(label)}</span>
      <span class="data count">${f.count}</span>
    </div>`;
  const sections = [...typed.keys()].sort().map((type) => `
    <h3>${escapeHtml(type)}</h3>
    ${typed.get(type).map((f) => row(f, f.name.slice(type.length + 1))).join("")}`);
  if (flat.length) sections.push(`${typed.size ? "<h3>Tags</h3>" : ""}${flat.map((f) => row(f, f.name)).join("")}`);
  return sections.join("");
}

function tabsHTML(active) {
  return `<nav class="app-nav" style="margin-bottom:14px;">${TABS.map(([key, label]) =>
    `<a href="#/browse/${key}" class="${key === active ? "active" : ""}">${label}</a>`).join("")}</nav>`;
}

export async function renderBrowse(tab, params) {
  const app = document.getElementById("app");
  tab = TABS.some(([k]) => k === tab) ? tab : "channels";
  app.innerHTML = `${tabsHTML(tab)}<div class="card"><div id="browseBody" class="spin">Loading</div></div>`;
  const el = document.getElementById("browseBody");

  try {
    if (tab === "extractors") {
      const which = params.get("extractor");
      if (which !== null) {
        // one extractor's things (the empty string = the NULL "unidentified" facet)
        const q = which === "" ? "extractor=" : `extractor=${encodeURIComponent(which)}`;
        const things = which === ""
          ? (await apiGet("/things/?limit=200")).filter((t) => !t.extractor_key)
          : await apiGet(`/things/?${q}&limit=200`);
        el.innerHTML = `<h2>${which ? escapeHtml(which) : "unidentified"}
            <span class="count">${things.length}</span></h2>`
          + (things.map((t) => thingRow(t, { extras: urlCell(t.url) })).join("")
             || `<div class="muted">Nothing here</div>`);
      } else {
        const facets = await apiGet("/things/facets");
        el.innerHTML = facets.map((f) => `
          <div class="item-row" tabindex="0"
               data-nav="#/browse/extractors?extractor=${encodeURIComponent(f.extractor_key ?? "")}">
            <span class="title">${f.extractor_key ? escapeHtml(f.extractor_key) : "<i>unidentified</i>"}</span>
            <span class="data count">${f.count}</span>
          </div>`).join("") || `<div class="muted">No things yet</div>`;
      }
    } else if (tab === "tags") {
      // LM tag explorer (#126) — distinct from #/tags, which searches OI *file* tags.
      const which = params.get("tag");
      if (which !== null) {
        const things = await apiGet(`/things/?tag=${encodeURIComponent(which)}&limit=200`);
        el.innerHTML = `<h2>${escapeHtml(which)} <span class="count">${things.length}</span></h2>`
          + (things.map((t) => thingRow(t, { extras: urlCell(t.url) })).join("")
             || `<div class="muted">Nothing tagged ${escapeHtml(which)} yet</div>`);
      } else {
        el.innerHTML = tagExplorerHTML(await apiGet("/tags/"));
      }
    } else if (tab === "channels") {
      const chans = await apiGet("/things/?kind=channel&limit=500");
      chans.sort((a, b) => (a.title || a.channel || "").localeCompare(b.title || b.channel || ""));
      el.innerHTML = chans.map((t) => thingRow(t, {
        extras: extractorChip(t.extractor_key) + urlCell(t.url),
      })).join("") || `<div class="muted">No channels discovered yet</div>`;
    } else {
      const query = { failing: "failing=true", new: "new=true&limit=200", due: "due=true&limit=200" }[tab];
      const things = await apiGet(`/things/?${query}`);
      el.innerHTML = things.map((t) => thingRow(t, {
        extras: extractorChip(t.extractor_key)
          + (tab === "failing" && t.last_failure_dt ? `<span class="muted">${fmtDt(t.last_failure_dt)}</span>` : "")
          + (tab === "due" && t.try_on ? `<span class="data muted">${t.try_on}</span>` : ""),
      })).join("") || `<div class="muted">Nothing here \u{1F389}</div>`;
    }
  } catch {
    el.classList.remove("spin");
    el.innerHTML = `<div class="error-box">Couldn't load — is the backend up?</div>`;
    return;
  }
  el.classList.remove("spin");
}
