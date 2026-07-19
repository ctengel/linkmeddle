/* App entry: route dispatch, global search, keyboard map (#134), help overlay, theme. */

import { setHandler, startRouter, parseHash } from "./router.js";
import { initActionDelegation } from "./util.js";
import { getVideoElement, navigatePlaylist, togglePiP, autoPlayEnabled,
         setAutoPlay } from "./player.js";
import { renderHome } from "./views/home.js";
import { renderManage } from "./views/manage.js";
import { renderAcquire, openOrAddUrl } from "./views/acquire.js";
import { renderThingPage, renderWatchSoon, renderOiFilePage } from "./views/thing.js";
import { renderBrowse } from "./views/browse.js";
import { renderOrganize } from "./views/organize.js";
import { renderSearch, renderTagSearch } from "./views/search.js";

/* === Routing === */

function dispatch() {
  const { parts, params, path } = parseHash();
  markActiveNav();

  if (parts[0] === "prefill") {
    // Bookmarklet target: land on Acquire with the captured URL pre-filled.
    renderAcquire(decodeURIComponent(path.replace(/^\/prefill\//, "")));
    return;
  }
  if (parts[0] === "manage") { renderManage(); return; }
  if (parts[0] === "acquire") { renderAcquire(); return; }
  if (parts[0] === "organize") { renderOrganize(params); return; }
  if (parts[0] === "watch-soon") { renderWatchSoon(); return; }
  if (parts[0] === "thing" && parts[1]) { renderThingPage(parts[1], params.get("ctx")); return; }
  if (parts[0] === "oi" && parts[1]) { renderOiFilePage(parts[1]); return; }
  if (parts[0] === "browse") { renderBrowse(parts[1], params); return; }
  if (parts[0] === "search") { renderSearch(params); return; }
  if (parts[0] === "tags") { renderTagSearch(params); return; }

  // Legacy hash redirects (V3 routes + the pre-4-mode Admin)
  if (parts[0] === "admin") {
    const qs = params.toString();
    location.replace(`#/organize${qs ? `?${qs}` : ""}`);
    return;
  }
  if (parts[0] === "playlist" && parts[1]) {
    location.replace(`#/thing/${parts[1]}`);
    return;
  }
  if (parts[0] === "video" && parts[1]) {
    const pl = params.get("playlist");
    location.replace(pl ? `#/thing/${parts[1]}?ctx=${pl}` : `#/thing/${parts[1]}`);
    return;
  }

  renderHome(params);
}

function markActiveNav() {
  const first = (parseHash().parts[0] || "");
  document.querySelectorAll(".app-nav a").forEach((a) => {
    const target = (a.getAttribute("href") || "").slice(2).split("/")[0];
    a.classList.toggle("active", target === first || (target === "" && first === ""));
  });
}

/* === Global search: URL paste -> resolve-or-add; anything else -> title search === */

document.getElementById("globalSearch").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("globalSearchInput");
  const q = input.value.trim();
  if (!q) return;
  if (/^https?:\/\//i.test(q)) {
    openOrAddUrl(q);
  } else {
    location.hash = `#/search?q=${encodeURIComponent(q)}`;
  }
  input.blur();
});

/* === Theme (dark-first; explicit choice persists and wins over the OS) === */

const savedTheme = localStorage.getItem("lm-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
document.getElementById("themeToggle").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("lm-theme", next);
});

/* === Keyboard shortcuts (#134) === */

document.addEventListener("keydown", (e) => {
  const inField = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  // Ctrl+F / "/" focus the search box from anywhere (the "ctrl+f" ask on #134)
  if ((e.key === "f" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !inField)) {
    e.preventDefault();
    document.getElementById("globalSearchInput").focus();
    return;
  }
  if (inField && e.key !== "?") return;
  const video = document.querySelector("#videoArea video") ? getVideoElement() : null;

  switch (e.key.toLowerCase()) {
    case " ":
    case "k":
      if (video) { e.preventDefault(); video.paused ? video.play() : video.pause(); }
      break;
    case "j":
      if (video) video.currentTime = Math.max(0, video.currentTime - 10);
      break;
    case "l":
      if (video) video.currentTime = Math.min(video.duration, video.currentTime + 10);
      break;
    case "arrowleft":  navigatePlaylist(-1); break;
    case "arrowright": navigatePlaylist(1);  break;
    case "f":
      if (video) {
        document.fullscreenElement ? document.exitFullscreen() : video.requestFullscreen();
      }
      break;
    case "m":
      if (video) video.muted = !video.muted;
      break;
    case "p": togglePiP(); break;
    case "home": location.hash = "#/"; break;
    case "?": toggleHelpOverlay(); break;
  }
});

/* === Help overlay === */

let helpVisible = false;

function buildHelpOverlay() {
  if (document.getElementById("shortcutOverlay")) return;
  const overlay = document.createElement("div");
  overlay.id = "shortcutOverlay";
  overlay.innerHTML = `
    <h2>Keyboard shortcuts</h2>
    <table>
      <tr><td>Space / K</td><td>Play / pause</td></tr>
      <tr><td>J / L</td><td>Back / forward 10 seconds</td></tr>
      <tr><td>&larr; &rarr;</td><td>Previous / next video</td></tr>
      <tr><td>F</td><td>Fullscreen</td></tr>
      <tr><td>P</td><td>Picture-in-Picture (stays on as the playlist advances)</td></tr>
      <tr><td>M</td><td>Mute</td></tr>
      <tr><td>/ or Ctrl+F</td><td>Search</td></tr>
      <tr><td>Home</td><td>Go to dashboard</td></tr>
      <tr><td>?</td><td>Toggle this help</td></tr>
    </table>
    <br/>
    <label><input type="checkbox" id="autoPlayToggle" ${autoPlayEnabled ? "checked" : ""}/> Auto-play next video</label>
    <br/><br/>
    <button id="helpClose">Close</button>
  `;
  document.body.appendChild(overlay);
  overlay.querySelector("#autoPlayToggle").addEventListener("change", (e) => setAutoPlay(e.target.checked));
  overlay.querySelector("#helpClose").addEventListener("click", toggleHelpOverlay);
}

function toggleHelpOverlay() {
  buildHelpOverlay();
  helpVisible = !helpVisible;
  document.getElementById("shortcutOverlay").style.display = helpVisible ? "block" : "none";
}

/* === Boot === */

initActionDelegation();
setHandler(dispatch);
startRouter();
