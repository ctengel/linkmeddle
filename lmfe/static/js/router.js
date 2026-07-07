/* Hash router. app.js supplies the dispatch function; views trigger `rerender()` after
   mutations that should redraw the current route (the old code called router() directly). */

let handler = null;

export function setHandler(fn) {
  handler = fn;
}

export function rerender() {
  handler?.();
}

export function startRouter() {
  window.addEventListener("hashchange", () => handler?.());
  handler?.();
}

/* Parse "#/route/arg?x=1" into { parts: ["route","arg"], params: URLSearchParams }. */
export function parseHash() {
  const hash = location.hash || "#/";
  const [path, query] = hash.slice(1).split("?");
  return {
    parts: path.split("/").filter(Boolean),
    params: new URLSearchParams(query || ""),
    path,
  };
}
