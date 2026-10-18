import { bodyOf, wfetch } from "./wire.js";

export const state = { repo: "proto/reviews", revision: "main", token: sessionStorage.getItem("lab-token") || "" };

const base = location.origin;
const headers = () => (state.token ? { Authorization: `Bearer ${state.token}` } : {});

export class ApiError extends Error {
  constructor(status, code, detail) {
    super(`${status} ${code}: ${detail}`);
    Object.assign(this, { status, code, detail });
  }
}

export async function json(path) {
  const r = await wfetch(`${base}${path}`, { headers: headers() }, path.split("?")[0].replace(/^\/api\/datasets\/[^/]+\/[^/]+\//, ""));
  const text = await bodyOf(r);
  let body = {};
  try { body = JSON.parse(text); } catch { /* the error branch below reports the text */ }
  if (!r.ok) throw new ApiError(r.status, r.headers.get("X-Error-Code") || "", body.detail?.error || body.error || text.slice(0, 200));
  return body;
}

// The viewer API: JSON in, hub-shaped errors out.
export function viewer(route, params = {}, repo = state.repo) {
  const q = new URLSearchParams({ revision: state.revision, ...params });
  return json(`/api/datasets/${repo}/viewer/${route}?${q}`);
}

export const resolveUrl = (path, repo = state.repo, rev = state.revision) => `${base}/datasets/${repo}/resolve/${rev}/${path}`;
export const listFiles = async (repo = state.repo) => (await json(`/api/datasets/${repo}/tree/${state.revision}?recursive=true`)).filter((e) => e.type === "file");
