// Every network call of the lab goes through wfetch, so the wire log shows what each screen really costs.
export const wire = [];
const listeners = new Set();
export const onWire = (fn) => listeners.add(fn);

// The log entry rides on the response (`r.entry`) so concurrent calls record their own byte counts.
export async function wfetch(url, opts = {}, tag = "") {
  const t0 = performance.now();
  const entry = { tag, method: opts.method || "GET", url, range: opts.headers?.Range || "", status: 0, ms: 0, bytes: 0 };
  wire.push(entry);
  try {
    const r = await fetch(url, opts);
    entry.status = r.status;
    r.entry = entry;
    return r;
  } finally {
    entry.ms = Math.round(performance.now() - t0);
    listeners.forEach((fn) => fn(entry));
  }
}

// Read a body and record its size on the entry.
export async function bodyOf(r, as = "text") {
  const body = await r[as]();
  r.entry.bytes = as === "text" ? new Blob([body]).size : body.byteLength;
  listeners.forEach((fn) => fn(r.entry));
  return body;
}

export const wireTotals = (from = 0) => {
  const rows = wire.slice(from);
  return { requests: rows.length, bytes: rows.reduce((a, e) => a + e.bytes, 0), ms: rows.reduce((a, e) => a + e.ms, 0) };
};
