// L4: DuckDB-WASM fed with bytes from our own Range reader. DuckDB's own HTTP layer is never used (it issues an
// un-ranged GET against any server, see the issue), so the browser downloads exactly what the budget allows.
import { readAll } from "./ranged.js";
import { resolveUrl, viewer } from "./api.js";

const DUCKDB = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";
let dbPromise;

export function duckdb() {
  dbPromise ||= (async () => {
    const lib = await import(DUCKDB);
    const bundle = await lib.selectBundle(lib.getJsDelivrBundles());
    const worker = new Worker(URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" })));
    const db = new lib.AsyncDuckDB(new lib.ConsoleLogger(lib.LogLevel.WARNING), worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    return db;
  })();
  return dbPromise;
}

// "select count(*) from <split>" is answered by the footers (exact, zero data bytes): no engine, no download.
const COUNT = /^\s*select\s+count\(\*\)\s+(?:as\s+\w+\s+)?from\s+"?([\w-]+)"?\s*;?\s*$/i;
export const footerAnswer = (sql) => sql.match(COUNT)?.[1] ?? null;

const loaded = new Map(); // `${repo}@${revision}/${config}/${split}` -> {approximate, files}

// Register one split as a view over the files the budget allows. Returns {approximate, loadedFiles, totalFiles, bytes}.
export async function ensureSplit(conn, db, resolution, config, split, budget, repo, revision) {
  const key = `${repo}@${revision}/${config}/${split}`;
  if (loaded.has(key)) return loaded.get(key);
  const s = resolution.configs.find((c) => c.config === config).splits.find((x) => x.split === split);
  let used = 0;
  const names = [];
  for (const [i, f] of s.files.entries()) {
    if (used + f.size > budget && names.length) break;
    if (used + f.size > budget) throw new Error(`${split}: the first file alone is ${f.size} bytes, over the browser budget of ${budget}`);
    const name = `${split}-${i}.${s.format === "jsonl" ? "json" : s.format}`;
    await db.registerFileBuffer(name, await readAll(resolveUrl(f.path, repo, revision), f.size, `sql/${split}`));
    names.push(name);
    used += f.size;
  }
  const reader = { parquet: "read_parquet", csv: "read_csv_auto", jsonl: "read_json_auto" }[s.format];
  await conn.query(`CREATE OR REPLACE VIEW "${split}" AS SELECT * FROM ${reader}([${names.map((n) => `'${n}'`).join(",")}])`);
  const info = { approximate: names.length < s.files.length, loadedFiles: names.length, totalFiles: s.files.length, bytes: used };
  loaded.set(key, info);
  return info;
}

export async function runSql(sql, resolution, config, budget, repo, revision) {
  const db = await duckdb();
  const conn = await db.connect();
  try {
    const infos = {};
    for (const c of resolution.configs.filter((c) => c.config === config))
      for (const s of c.splits)
        if (new RegExp(`\\b${s.split}\\b`).test(sql)) infos[s.split] = await ensureSplit(conn, db, resolution, config, s.split, budget, repo, revision);
    const t0 = performance.now();
    const result = await conn.query(sql);
    const columns = result.schema.fields.map((f) => f.name);
    const rows = result.toArray().map((r) => columns.map((c) => { const v = r[c]; return typeof v === "bigint" ? Number(v) : v; }));
    return { columns, rows, infos, ms: Math.round(performance.now() - t0) };
  } finally {
    await conn.close();
  }
}

export const exactCount = async (split, config = "default") => {
  const size = await viewer("size", { config });
  const e = size.splits.find((x) => x.split === split);
  if (!e) throw new Error(`no split called ${split}`);
  return e.num_rows;
};
