"""Sample the repositories named in selection.json from the real Hub, once, into test/fixtures/hub_corpus.

By hand only (CI never runs it): slow, one request at a time, revisions pinned in manifest.json.
Kept: the files of ``common.keep`` and, of each .safetensors file, its header only. Everything else
(weights above all) is left on the Hub. Usage: python scripts/corpus/sample.py [--only ID ...]
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download, hf_hub_url
from huggingface_hub.utils import get_session, build_hf_headers

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.corpus.common import MAX_FILE, is_safetensors, keep, pack, pause, sha256  # noqa: E402

HERE = Path(__file__).parent
CORPUS = HERE.parents[1] / "test" / "fixtures" / "hub_corpus"
MAX_HEADER = 16 * 1024 * 1024
MAX_FILES = 60
MAX_REPO = 4 * 1000 * 1000  # kept bytes of one repository: the corpus is in git, and breadth beats weight


def alias(repo_id: str) -> str:
    return repo_id.replace("/", "__")


def header_bytes(repo_id: str, path: str, revision: str, repo_type: str) -> bytes | None:
    url = hf_hub_url(repo_id, path, revision=revision, repo_type=repo_type)
    session, headers = get_session(), build_hf_headers()
    head = session.get(url, headers={**headers, "Range": "bytes=0-7"}, timeout=60)
    pause()
    if head.status_code not in (200, 206) or len(head.content) < 8:
        return None
    (size,) = struct.unpack("<Q", head.content[:8])
    if size > MAX_HEADER:
        return None
    body = session.get(url, headers={**headers, "Range": f"bytes=0-{8 + size - 1}"}, timeout=120)
    pause()
    return body.content[: 8 + size] if len(body.content) >= 8 + size else None


def sample(api: HfApi, repo_id: str, repo_type: str, cache: str) -> dict:
    info = api.repo_info(repo_id, repo_type=repo_type)
    card = getattr(info, "card_data", None)
    license_ = card.get("license") if card else None
    stage = tempfile.TemporaryDirectory()  # packed into one zip when the repository is complete
    root = Path(stage.name)
    files = []
    tree = [item for item in api.list_repo_tree(repo_id, repo_type=repo_type, revision=info.sha, recursive=True) if hasattr(item, "size")]
    if sum(1 for item in tree if keep(item.path) or is_safetensors(item.path)) > MAX_FILES:
        raise ValueError(f"more than {MAX_FILES} files: too many requests for a polite sampler")
    for item in tree:
        target = root / item.path
        if is_safetensors(item.path) and repo_type == "model":
            data = header_bytes(repo_id, item.path, info.sha, repo_type)
            if data is None:
                continue
            entry = {"header_only": True, "full_size": item.size}
        elif keep(item.path) and item.size <= MAX_FILE:
            local = hf_hub_download(repo_id, item.path, revision=info.sha, repo_type=repo_type, cache_dir=cache)
            pause()
            data = Path(local).read_bytes()
            entry = {"header_only": False, "full_size": item.size}
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": item.path, "size": len(data), "sha256": sha256(data), **entry})
    if sum(f["size"] for f in files) > MAX_REPO:
        stage.cleanup()
        raise ValueError(f"more than {MAX_REPO} bytes of kept files")
    pack(root, CORPUS / "repos" / repo_type / f"{alias(repo_id)}.zip")
    stage.cleanup()
    return {"id": repo_id, "type": repo_type, "revision": info.sha, "license": license_, "files": sorted(files, key=lambda f: f["path"])}


def enrich(manifest: dict, api: HfApi) -> None:
    """Record what the Hub says about each sampled repository at its sampled revision (the oracle of tests)."""
    for entry in manifest["repos"].values():
        if "hub" in entry:
            continue
        if entry["type"] == "model":
            info = api.model_info(
                entry["id"],
                revision=entry["revision"],
                expand=["pipeline_tag", "library_name", "tags", "safetensors", "config"],
            )
            safetensors = getattr(info, "safetensors", None)
            entry["hub"] = {
                "pipeline_tag": info.pipeline_tag,
                "library_name": info.library_name,
                "tags": list(info.tags or []),
                "model_type": (info.config or {}).get("model_type"),
                "safetensors": {"total": safetensors.total, "parameters": dict(safetensors.parameters)} if safetensors else None,
            }
        else:
            info = api.dataset_info(entry["id"], revision=entry["revision"], expand=["tags"])
            entry["hub"] = {"tags": list(info.tags or [])}
        pause()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prune", action="store_true", help="drop sampled repositories over the size budget")
    parser.add_argument("--enrich", action="store_true", help="add the Hub's own words about each sampled repository")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--workers", type=int, default=6)  # requests are latency-bound, not bandwidth-bound
    args = parser.parse_args()
    selection = json.loads((HERE / "selection.json").read_text())
    manifest_path = CORPUS / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"repos": {}}
    api = HfApi()
    if args.prune:
        for name, entry in list(manifest["repos"].items()):
            if sum(f["size"] for f in entry["files"]) > MAX_REPO or len(entry["files"]) > MAX_FILES:
                (CORPUS / "repos" / f"{name}.zip").unlink(missing_ok=True)
                del manifest["repos"][name]
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        return
    if args.enrich:
        enrich(manifest, api)
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        return
    lock = threading.Lock()
    todo = [
        (repo_type, repo_id)
        for repo_type, key in (("model", "models"), ("dataset", "datasets"))
        for repo_id in selection[key]
        if (not args.only or repo_id in args.only) and f"{repo_type}/{alias(repo_id)}" not in manifest["repos"]
    ]
    with tempfile.TemporaryDirectory() as cache:

        def work(item):
            repo_type, repo_id = item
            name = f"{repo_type}/{alias(repo_id)}"
            try:
                entry = sample(api, repo_id, repo_type, cache)
            except Exception as error:  # a repository that went away or turned gated is skipped
                print("skipped", repo_id, type(error).__name__, error, file=sys.stderr, flush=True)
                return
            with lock:
                manifest["repos"][name] = entry
                CORPUS.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
            print("sampled", name, sum(f["size"] for f in entry["files"]), file=sys.stderr, flush=True)

        with ThreadPoolExecutor(args.workers) as pool:
            list(pool.map(work, todo))


if __name__ == "__main__":
    main()
