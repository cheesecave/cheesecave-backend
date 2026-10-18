// L1: a hyparquet AsyncBuffer (and a plain byte reader) over HTTP Range, counted by the wire log.
import { bodyOf, wfetch } from "./wire.js";

export async function readRange(url, start, endExclusive, tag = "range") {
  const r = await wfetch(url, { headers: { Range: `bytes=${start}-${endExclusive - 1}` } }, tag);
  if (r.status !== 206 && r.status !== 200) throw new Error(`range read answered HTTP ${r.status}`);
  const buf = await bodyOf(r, "arrayBuffer");
  return r.status === 200 ? buf.slice(start, endExclusive) : buf;
}

export const asyncBuffer = (url, size, tag = "parquet") => ({
  byteLength: size,
  slice: (a, b = size) => readRange(url, a, Math.min(b, size), tag),
});

export async function readAll(url, size, tag = "download") {
  return new Uint8Array(await readRange(url, 0, size, tag));
}
