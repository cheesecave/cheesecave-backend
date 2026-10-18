import { h, fmtBytes, badge, table, costLine } from "./js/dom.js";
import { state, viewer, json, listFiles, resolveUrl, ApiError } from "./js/api.js";
import { wire, onWire, wireTotals } from "./js/wire.js";
import { readRange } from "./js/ranged.js";
import { readRows, fileOfRow, open as openParquet } from "./js/parquet.js";
import { runSql, footerAnswer, exactCount } from "./js/sql.js";

const REPOS = ["proto/reviews", "proto/images-parquet", "proto/mixed-formats", "proto/webds", "proto/versions", "proto/private-data"];
const PAGES = { data: "Data tab", file: "File explorer", sql: "SQL", archive: "Archive / WebDataset", diff: "Revision diff", snippets: "Copy to code", readme: "README card" };
const $ = (id) => document.getElementById(id);
const timed = async (fn) => { const t = performance.now(); const v = await fn(); return [v, Math.round(performance.now() - t)]; };
const fail = (e) => h("div", { class: "err", "data-testid": "error" }, e instanceof ApiError ? `${e.status} ${e.code}: ${e.detail}` : String(e.message || e));

// ---------------------------------------------------------------------------------------------- shell
const q = new URLSearchParams(location.hash.split("?")[1] || "");
const pageName = () => location.hash.replace(/^#\/?/, "").split("?")[0] || "data";
state.repo = q.get("repo") || state.repo;
state.revision = q.get("revision") || state.revision;

$("repo").append(...REPOS.map((r) => h("option", { value: r, selected: r === state.repo }, r)));
$("revision").value = state.revision;
$("token").value = state.token;
$("repo").onchange = (e) => { state.repo = e.target.value; render(); };
$("revision").onchange = (e) => { state.revision = e.target.value || "main"; render(); };
$("token").onchange = (e) => { state.token = e.target.value; sessionStorage.setItem("lab-token", state.token); render(); };
$("wireclear").onclick = (e) => { e.preventDefault(); wire.length = 0; $("wire").replaceChildren(); $("wiretotal").textContent = ""; };
onWire(() => {
  const t = wireTotals();
  $("wiretotal").textContent = `${t.requests} requests · ${fmtBytes(t.bytes)}`;
  $("wire").replaceChildren(...wire.slice(-60).map((e) => h("tr", {}, h("td", {}, e.status || "…"), h("td", {}, e.method), h("td", {}, e.tag), h("td", {}, e.range), h("td", {}, fmtBytes(e.bytes)), h("td", {}, `${e.ms} ms`), h("td", {}, e.url.replace(location.origin, "")))));
});

function nav() {
  $("nav").replaceChildren(...Object.entries(PAGES).map(([k, v]) => h("a", { href: `#/${k}`, class: pageName() === k ? "on" : "", "data-testid": `nav-${k}` }, v)));
}

async function render() {
  nav();
  const main = $("main");
  main.replaceChildren(h("div", { class: "muted" }, "loading…"));
  const page = { data: dataPage, file: filePage, sql: sqlPage, archive: archivePage, diff: diffPage, snippets: snippetsPage, readme: readmePage }[pageName()] || dataPage;
  try { main.replaceChildren(await page()); } catch (e) { main.replaceChildren(fail(e)); }
}
addEventListener("hashchange", render);

// ---------------------------------------------------------------------------------------------- shared pieces
const resolution = async () => viewer("splits");
const labelOf = (features, name, v) => {
  const f = features.find((x) => x.name === name);
  return f?.type?._type === "ClassLabel" && Number.isInteger(v) ? f.type.names[v] ?? v : v;
};

function cell(features, name, v) {
  if (v == null) return h("span", { class: "null" }, "null");
  if (v && v._type === "binary-struct") return h("img", { class: "thumb", alt: v.path, "data-col": name, "data-testid": "thumb" });
  if (typeof v === "object") return JSON.stringify(v);
  return String(labelOf(features, name, v));
}

// Thumbnails come from the browser's own Range reads of the parquet column, not from the viewer API.
async function fillThumbs(root, res, split, config, offset) {
  const imgs = [...root.querySelectorAll("img.thumb")];
  if (!imgs.length) return;
  const cfg = res.configs.find((c) => c.config === config);
  const files = cfg.splits.find((s) => s.split === split).files;
  const col = imgs[0].dataset.col;
  const { file, local } = await fileOfRow(files, offset, state.repo, state.revision);
  const { rows } = await readRows(file, state.repo, state.revision, local, local + imgs.length, [col], { utf8: false });
  imgs.forEach((img, i) => { const b = rows[i]?.[col]?.bytes; if (b) img.src = URL.createObjectURL(new Blob([b])); });
}

function statsPanel(s) {
  const body = s.statistics.map((c) => {
    const cs = c.column_statistics || {};
    const bars = cs.frequencies ? Object.entries(cs.frequencies) : cs.histogram ? cs.histogram.hist.map((n, i) => [String(cs.histogram.bin_edges?.[i] ?? i), n]) : [];
    const max = Math.max(1, ...bars.map(([, n]) => n));
    const scalars = Object.entries(cs).filter(([k, v]) => typeof v !== "object" && v !== null && k !== "exact_from_footer").map(([k, v]) => `${k}=${typeof v === "number" ? +v.toFixed(3) : v}`).join(" · ");
    return h("div", { class: "panel", "data-testid": `stat-${c.column_name}` },
      h("b", {}, c.column_name), " ", badge(c.column_type), cs.exact_from_footer ? badge("exact · footer", "ok") : badge(s.partial ? "partial sample" : `sample of ${s.sampled_rows}`, "warn"),
      h("div", { class: "muted" }, scalars),
      bars.map(([k, n]) => h("div", { class: "bar-h" }, h("span", { style: "width:90px;overflow:hidden" }, k), h("i", { style: `width:${Math.round((n / max) * 160)}px` }), n)));
  });
  return h("div", { "data-testid": "stats" }, h("div", { class: "muted" }, `${s.num_examples} examples · histograms from ${s.sampled_rows} sampled rows`), body);
}

// ---------------------------------------------------------------------------------------------- 1. dataset Data tab
async function dataPage() {
  const res = await resolution();
  let config = res.configs.find((c) => c.default)?.config || res.configs[0].config;
  let split = res.configs[0].splits[0].split;
  let offset = 0, mode = "rows", term = "";
  const LENGTH = 25;
  const out = h("div", {});
  const body = h("div", {});
  const stats = h("div", {});
  const controls = h("div", { class: "row" });

  const load = async () => {
    body.replaceChildren(h("div", { class: "muted" }, "loading…"));
    const route = mode === "rows" ? "rows" : mode;
    const params = { config, split, offset, length: LENGTH };
    if (mode === "filter") params.where = term;
    if (mode === "search") params.query = term;
    try {
      const [j, ms] = await timed(() => viewer(route, params));
      const total = j.num_rows_total;
      body.replaceChildren(
        h("div", { class: "row" },
          badge(`${total ?? "≥" + (offset + j.rows.length)} rows`, j.partial ? "warn" : "ok", "total"), j.partial ? badge("partial scan: total unknown", "warn", "partial") : null,
          h("button", { type: "button", "data-testid": "prev", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - LENGTH); load(); } }, "‹ prev"),
          h("button", { type: "button", "data-testid": "next", disabled: j.rows.length < LENGTH || (total != null && offset + LENGTH >= total), onclick: () => { offset += LENGTH; load(); } }, "next ›"),
          h("span", { class: "muted", "data-testid": "range" }, `rows ${j.rows[0]?.row_idx ?? "–"}–${j.rows.at(-1)?.row_idx ?? "–"}`)),
        costLine(j.cost, ms, `commit ${j.commit.slice(0, 8)}`),
        table(["#", ...j.features.map((f) => f.name)], j.rows.map((r) => [r.row_idx, ...j.features.map((f) => r.row[f.name])]),
          (v, i) => (i === 0 ? v : cell(j.features, j.features[i - 1].name, v))),
        h("details", {}, h("summary", {}, "schema"), table(["column", "type"], j.features.map((f) => [f.name, f.type._type === "Value" ? f.type.dtype : f.type._type === "ClassLabel" ? `ClassLabel(${f.type.names.length})` : f.type._type]))));
      body.querySelector("table.grid").dataset.testid = "rows";
      fillThumbs(body, res, split, config, offset).catch((e) => console.error("thumbnails:", e));
    } catch (e) { body.replaceChildren(fail(e)); }
  };

  const loadStats = async () => {
    stats.replaceChildren(h("div", { class: "muted" }, "computing…"));
    try {
      const [s, ms] = await timed(() => viewer("statistics", { config, split }));
      stats.replaceChildren(statsPanel(s), h("div", { class: "cost" }, `${ms} ms`));
    } catch (e) { stats.replaceChildren(fail(e)); }
  };

  const draw = () => {
    const cfg = res.configs.find((c) => c.config === config);
    const cur = cfg.splits.find((s) => s.split === split) || cfg.splits[0];
    split = cur.split;
    const input = h("input", { size: 40, "data-testid": "term", placeholder: mode === "filter" ? `"score" > 3.5 AND "label" = 4` : "search text", value: term });
    controls.replaceChildren(...[
      h("select", { "data-testid": "config", onchange: (e) => { config = e.target.value; offset = 0; draw(); load(); loadStats(); } }, res.configs.map((c) => h("option", { value: c.config, selected: c.config === config }, c.config))),
      h("select", { "data-testid": "split", onchange: (e) => { split = e.target.value; offset = 0; draw(); load(); loadStats(); } }, cfg.splits.map((s) => h("option", { value: s.split, selected: s.split === split }, s.split))),
      badge(cur.format, "", "format"), badge(res.source === "yaml" ? "from README configs" : "from file names", "", "source"),
      h("select", { "data-testid": "mode", onchange: (e) => { mode = e.target.value; draw(); } }, ["rows", "filter", "search"].map((m) => h("option", { value: m, selected: m === mode }, m))),
      mode === "rows" ? null : input,
      mode === "rows" ? null : h("button", { type: "button", class: "primary", "data-testid": "go", onclick: () => { term = input.value; offset = 0; load(); } }, mode),
      h("span", { class: "muted" }, `${cur.files.length} file(s), ${fmtBytes(cur.files.reduce((a, f) => a + f.size, 0))}`)].filter(Boolean));
    if (mode === "rows") { term = ""; offset = offset; }
  };

  draw();
  out.append(controls, h("div", { class: "cols" }, body, h("div", {}, h("h4", {}, "Statistics"), stats)));
  load(); loadStats();
  return out;
}

// ---------------------------------------------------------------------------------------------- 2. single file, client only
async function filePage() {
  const files = (await listFiles()).filter((f) => /\.(parquet)$/i.test(f.path));
  if (!files.length) return h("div", { class: "muted", "data-testid": "nofile" }, "no parquet file in this repository");
  const out = h("div", {});
  const body = h("div", { "data-testid": "filebody" });
  const draw = async (file) => {
    const t0 = wire.length;
    body.replaceChildren(h("div", { class: "muted" }, "reading footer…"));
    const [{ metadata, rows }, ms] = await timed(async () => { const { metadata } = await openParquet(file, state.repo, state.revision); return readRows(file, state.repo, state.revision, 0, 20); });
    const t = wireTotals(t0);
    const cols = Object.keys(rows[0] || {});
    setTimeout(() => body.querySelector("table.grid")?.setAttribute("data-testid", "rows"));
    body.replaceChildren(
      h("div", { class: "row" }, badge("client only: zero viewer API calls", "ok", "clientonly"), badge(`${metadata.num_rows} rows`), badge(`${metadata.row_groups.length} row groups`), h("span", { class: "muted" }, metadata.created_by)),
      h("div", { class: "cost", "data-testid": "cost" }, `${t.requests} range requests · ${fmtBytes(t.bytes)} read of ${fmtBytes(file.size)} · ${ms} ms`),
      table(cols, rows.map((r) => cols.map((c) => r[c])), (v) => (v instanceof Uint8Array ? `${v.length} bytes` : v == null ? h("span", { class: "null" }, "null") : typeof v === "object" ? JSON.stringify(v, (k, x) => (typeof x === "bigint" ? Number(x) : x instanceof Uint8Array ? `${x.length} bytes` : x)) : String(v))));
  };
  out.append(h("div", { class: "row" }, h("select", { "data-testid": "filepick", onchange: (e) => draw(files.find((f) => f.path === e.target.value)) }, files.map((f) => h("option", { value: f.path }, `${f.path} (${fmtBytes(f.size)})`)))), body);
  draw(files[0]).catch((e) => body.replaceChildren(fail(e)));
  return out;
}

// ---------------------------------------------------------------------------------------------- 3. SQL console
async function sqlPage() {
  const res = await resolution();
  const config = res.configs[0].config;
  const splits = res.configs[0].splits.map((s) => s.split);
  const first = splits[0];
  const ta = h("textarea", { "data-testid": "sql" }, `SELECT label, count(*) AS n, round(avg(score), 3) AS avg_score\nFROM ${first}\nGROUP BY label ORDER BY label`);
  const budget = h("input", { type: "number", value: 64, size: 4, "data-testid": "budget" });
  const out = h("div", { "data-testid": "sqlresult" });
  const run = async () => {
    const sql = ta.value;
    out.replaceChildren(h("div", { class: "muted" }, "running…"));
    const t0 = wire.length;
    try {
      const only = footerAnswer(sql);
      if (only) {
        const [n, ms] = await timed(() => exactCount(only, config));
        out.replaceChildren(h("div", { class: "row" }, badge("answered from parquet footers: exact, no data bytes", "ok", "footer")), table(["count_star()"], [[n]]), h("div", { class: "cost", "data-testid": "cost" }, `${wireTotals(t0).requests} request · ${ms} ms · no SQL engine was loaded`));
        return;
      }
      const budgetBytes = Number(budget.value) * 1048576;
      const [r, ms] = await timed(() => runSql(sql, res, config, budgetBytes, state.repo, state.revision));
      const infos = Object.entries(r.infos);
      const approx = infos.some(([, i]) => i.approximate);
      out.replaceChildren(
        h("div", { class: "row" }, approx ? badge("approximate: not all files fit the budget", "warn", "approx") : badge("exact: every file of the split was read", "ok", "exact"),
          infos.map(([s, i]) => badge(`${s}: ${i.loadedFiles}/${i.totalFiles} files · ${fmtBytes(i.bytes)}`))),
        table(r.columns, r.rows.slice(0, 200)),
        h("div", { class: "cost", "data-testid": "cost" }, `${r.rows.length} row(s) · query ${r.ms} ms · total ${ms} ms · ${fmtBytes(wireTotals(t0).bytes)} over the wire`));
    } catch (e) { out.replaceChildren(fail(e)); }
  };
  return h("div", {}, h("div", { class: "muted" }, `Tables: ${splits.map((s) => `"${s}"`).join(", ")} (config ${config}). DuckDB-WASM runs in your browser over bytes we read with Range.`), ta,
    h("div", { class: "row" }, h("button", { type: "button", class: "primary", "data-testid": "run", onclick: run }, "Run"), h("label", {}, "browser budget (MiB) ", budget)), out);
}

// ---------------------------------------------------------------------------------------------- 4. archives
async function archivePage() {
  const files = (await listFiles()).filter((f) => f.path.toLowerCase().endsWith(".tar"));
  if (!files.length) return h("div", { class: "muted", "data-testid": "noarchive" }, "no .tar file in this repository");
  const body = h("div", { "data-testid": "archivebody" });
  const draw = async (file) => {
    body.replaceChildren(h("div", { class: "muted" }, "walking tar headers…"));
    const [j, ms] = await timed(() => viewer("tar-index", { path: file.path, limit: 12 }));
    const grid = h("div", { class: "samples", "data-testid": "samples" }, j.samples.map((s) => {
      const img = s.members.find((m) => /^(jpe?g|png|webp)$/.test(m.ext));
      const txt = s.members.find((m) => m.ext === "txt" || m.ext === "json");
      const el = h("div", { class: "sample", "data-testid": "sample" }, img ? h("img", { alt: s.key }) : null, h("b", {}, s.key), h("div", { class: "muted caption" }, ""), h("div", {}, s.members.map((m) => badge(`${m.ext} ${fmtBytes(m.size)}`))));
      const url = resolveUrl(file.path);
      if (img) readRange(url, img.offset, img.offset + img.size, "member").then((b) => { el.querySelector("img").src = URL.createObjectURL(new Blob([b])); });
      if (txt) readRange(url, txt.offset, txt.offset + txt.size, "member").then((b) => { el.querySelector(".caption").textContent = new TextDecoder().decode(b).slice(0, 120); });
      return el;
    }));
    body.replaceChildren(
      h("div", { class: "row" }, badge(`${j.members} members`), badge(`${j.num_samples} samples`, "ok", "nsamples"), j.truncated ? badge("truncated", "warn") : null),
      costLine(j.cost, ms, `${fmtBytes(j.file_size)} archive`), grid);
  };
  const first = files.find((f) => f.path.includes("shard-")) || files[0];
  draw(first).catch((e) => body.replaceChildren(fail(e)));
  return h("div", {}, h("div", { class: "row" }, h("select", { "data-testid": "tarpick", onchange: (e) => draw(files.find((f) => f.path === e.target.value)) }, files.map((f) => h("option", { value: f.path, selected: f === first }, `${f.path} (${fmtBytes(f.size)})`)))), body);
}

// ---------------------------------------------------------------------------------------------- 5. revision diff
async function diffPage() {
  let commits = [];
  try { commits = (await json(`/api/datasets/${state.repo}/commits/${state.revision}`)).map((c) => ({ id: c.id, title: c.title })); } catch { /* manual entry still works */ }
  const files = (await listFiles()).filter((f) => f.path.endsWith(".parquet"));
  const old = h("input", { size: 42, "data-testid": "old", value: q.get("old") || commits.at(-1)?.id || "" });
  const neu = h("input", { size: 42, "data-testid": "new", value: q.get("new") || commits[0]?.id || "main" });
  const key = h("input", { size: 8, "data-testid": "key", value: q.get("key") || "id" });
  const path = h("select", { "data-testid": "diffpath" }, files.map((f) => h("option", { value: f.path }, f.path)));
  const out = h("div", { "data-testid": "diffresult" });
  const run = async () => {
    out.replaceChildren(h("div", { class: "muted" }, "diffing…"));
    try {
      const [j, ms] = await timed(() => viewer("diff", { path: path.value, old: old.value, new: neu.value, ...(key.value ? { key: key.value } : {}) }));
      const stat = (label, v, id) => h("div", { class: "stat", "data-testid": id }, h("span", { class: "muted" }, label), h("b", {}, v));
      out.replaceChildren(
        j.identical_content ? badge("identical objects: nothing was read", "ok", "identical") : null,
        h("div", { class: "diffgrid" }, stat("rows old → new", `${j.rows.old} → ${j.rows.new}`, "rows"), stat("delta", (j.rows.delta > 0 ? "+" : "") + j.rows.delta, "delta"),
          j.keyed ? [stat("added", j.added, "added"), stat("removed", j.removed, "removed"), stat("changed", j.changed, "changed")] : null),
        h("div", { class: "row" }, "schema:", j.schema.added.map((c) => badge(`+ ${c}`, "ok")), j.schema.removed.map((c) => badge(`− ${c}`, "bad")), !j.schema.added.length && !j.schema.removed.length ? badge("unchanged") : null,
          Object.keys(j.changed_columns || {}).length ? ["changed columns:", Object.entries(j.changed_columns).map(([c, n]) => badge(`${c} ×${n}`, "warn"))] : null),
        j.examples?.changed?.length ? h("div", { class: "panel", "data-testid": "examples" }, h("b", {}, "examples of changed rows"),
          table([j.key, "column", "old", "new"], j.examples.changed.flatMap((r) => Object.entries(r).filter(([k]) => k !== j.key).map(([c, v]) => [r[j.key], c, v.old, v.new])))) : null,
        h("div", { class: "cost" }, `${ms} ms · old ${j.old.slice(0, 8)} → new ${j.new.slice(0, 8)}`));
    } catch (e) { out.replaceChildren(fail(e)); }
  };
  const box = h("div", {}, h("div", { class: "row" }, path, "old", old, "new", neu, "key", key, h("button", { type: "button", class: "primary", "data-testid": "diffgo", onclick: run }, "Diff")),
    commits.length ? h("div", { class: "muted" }, "commits: " + commits.map((c) => `${c.id.slice(0, 8)} ${c.title}`).join(" | ")) : null, out);
  if (q.get("old") || commits.length > 1) run();
  return box;
}

// ---------------------------------------------------------------------------------------------- 6. copy to code
async function snippetsPage() {
  const res = await resolution();
  const cfg = res.configs[0];
  const s = cfg.splits[0];
  const urls = s.files.map((f) => resolveUrl(f.path));
  const L = (a) => JSON.stringify(a);
  const reader = { parquet: "read_parquet", csv: "read_csv_auto", jsonl: "read_json_auto" }[s.format] || "read_parquet";
  const snippets = {
    duckdb: `import duckdb\nprint(duckdb.sql("""SELECT count(*) FROM ${reader}([${urls.map((u) => `'${u}'`).join(", ")}])""").fetchone()[0])`,
    pandas: s.format === "parquet" ? `import pandas as pd\ndf = pd.concat([pd.read_parquet(u) for u in ${L(urls)}])\nprint(len(df))` : `import pandas as pd\ndf = pd.concat([pd.read_${s.format === "jsonl" ? "json" : "csv"}(u${s.format === "jsonl" ? ", lines=True" : ""}) for u in ${L(urls)}])\nprint(len(df))`,
    polars: `import polars as pl\nprint(pl.concat([pl.read_${s.format === "jsonl" ? "ndjson" : s.format}(u) for u in ${L(urls)}]).height)`,
    datasets: `from datasets import load_dataset\nds = load_dataset(${L(s.format === "jsonl" ? "json" : s.format)}, data_files={${L(s.split)}: ${L(urls)}})\nprint(ds[${L(s.split)}].num_rows)`,
    viewer: `curl "${location.origin}/api/datasets/${state.repo}/viewer/rows?config=${cfg.config}&split=${s.split}&offset=0&length=5"`,
  };
  return h("div", { "data-testid": "snippets" }, h("div", { class: "muted" }, `First split of ${state.repo}: ${s.split} (${s.format}). Each snippet is executed against this hub by the test suite.`),
    Object.entries(snippets).map(([k, v]) => h("div", { class: "panel" }, h("b", {}, k), " ", h("button", { type: "button", onclick: () => navigator.clipboard?.writeText(v) }, "copy"), h("pre", { "data-testid": `snippet-${k}` }, v))));
}

// ---------------------------------------------------------------------------------------------- 7. README card
async function readmePage() {
  const readme = await (await fetch(resolveUrl("README.md"), { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} })).text();
  const res = await resolution();
  const s = res.configs[0].splits[0];
  const j = await viewer("rows", { config: res.configs[0].config, split: s.split, offset: 0, length: 4 });
  return h("div", { class: "readme", "data-testid": "readmecard" }, h("pre", {}, readme),
    h("div", { class: "card" }, h("b", {}, "Dataset preview"), " ", badge(`${s.split} · ${j.num_rows_total ?? "?"} rows`), table(j.features.map((f) => f.name), j.rows.map((r) => j.features.map((f) => r.row[f.name])), (v) => (v && v._type === "binary-struct" ? "image" : v == null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v))),
      h("a", { href: "#/data", "data-testid": "viewall" }, "View all rows →")));
}

render();
