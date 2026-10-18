// L1: parquet read in the browser with hyparquet (MIT) over our Range reader. No viewer API involved.
import { parquetMetadataAsync, parquetReadObjects } from "https://esm.sh/hyparquet@1.17.1";
// hyparquet ships snappy only; real hub datasets use zstd, gzip, lz4 and brotli too (hyparquet-compressors, MIT).
import { compressors } from "https://esm.sh/hyparquet-compressors@1.1.2";
import { asyncBuffer } from "./ranged.js";
import { resolveUrl } from "./api.js";

const metaCache = new Map();

export async function open(file, repo, revision) {
  const url = resolveUrl(file.path, repo, revision);
  const buf = asyncBuffer(url, file.size, "parquet");
  const key = `${url}#${file.size}`;
  if (!metaCache.has(key)) metaCache.set(key, await parquetMetadataAsync(buf));
  return { buf, metadata: metaCache.get(key) };
}

// utf8:false keeps BYTE_ARRAY as bytes (hyparquet decodes them to strings by default, which corrupts image columns).
export async function readRows(file, repo, revision, rowStart, rowEnd, columns, { utf8 = true } = {}) {
  const { buf, metadata } = await open(file, repo, revision);
  return { metadata, rows: await parquetReadObjects({ file: buf, metadata, rowStart, rowEnd, columns, compressors, utf8 }) };
}

// Find which file of a split holds a global row index, from the footers (a Range read each, cached).
export async function fileOfRow(files, index, repo, revision) {
  let before = 0;
  for (const f of files) {
    const { metadata } = await open(f, repo, revision);
    const n = Number(metadata.num_rows);
    if (index < before + n) return { file: f, local: index - before };
    before += n;
  }
  throw new Error(`row ${index} is past the end of the split`);
}
