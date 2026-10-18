// replaceChildren would print null/false as text and not flatten nested lists; h() already does both.
const replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...kids) { return replaceChildren.apply(this, kids.flat(Infinity).filter((k) => k != null && k !== false)); };

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

export const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 1048576).toFixed(1)} MiB`);
export const badge = (text, kind = "", testid = "") => h("span", { class: `badge ${kind}`, "data-testid": testid || null }, text);

export function table(columns, rows, cell = (v) => (v == null ? h("span", { class: "null" }, "null") : typeof v === "object" ? JSON.stringify(v) : String(v))) {
  return h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v, i) => h("td", {}, cell(v, i)))))));
}

export function costLine(cost, ms, extra = "") {
  const parts = [];
  if (cost) parts.push(`${cost.range_requests} range request${cost.range_requests === 1 ? "" : "s"} to storage · ${fmtBytes(cost.bytes_read)} read`);
  if (ms != null) parts.push(`${ms} ms`);
  if (extra) parts.push(extra);
  return h("div", { class: "cost", "data-testid": "cost" }, parts.join(" · "));
}
