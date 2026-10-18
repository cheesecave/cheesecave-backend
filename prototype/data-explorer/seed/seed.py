"""Seed the prototype stack with synthetic datasets through the REAL huggingface_hub client.

Synthetic data only (no real dataset content is used anywhere in the prototype).
usage: seed.py <base url> <out dir>
Writes <out dir>/truth.json (ground truth computed from the files themselves) and <out dir>/tokens.json (local only).
"""
import io
import json
import os
import random
import sys
import tarfile

import httpx
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image

BASE, OUT = sys.argv[1].rstrip("/"), sys.argv[2]
os.makedirs(OUT, exist_ok=True)
os.environ["HF_ENDPOINT"] = BASE
os.environ["HF_HUB_DISABLE_XET"] = "1"
from huggingface_hub import CommitOperationAdd, HfApi  # noqa: E402

rng = random.Random(20261018)
np_rng = np.random.default_rng(20261018)
PASSWORD = "proto-password-123"
WORDS = ("cheese cave dataset viewer parquet shard split model token image label review quality speed latency "
         "storage object version commit branch index range column row group schema stats").split()
LABELS = ["very negative", "negative", "neutral", "positive", "very positive"]


def users():
    http = httpx.Client(base_url=BASE, timeout=60, trust_env=False)
    tokens = {}
    for name in ("proto", "other"):
        http.post("/api/auth/register", json={"username": name, "email": f"{name}@example.com", "password": PASSWORD})
        r = http.post("/api/auth/login", json={"username": name, "password": PASSWORD})
        r.raise_for_status()
        tokens[name] = http.post("/api/auth/tokens/create", json={"name": "seed"}).json()["token"]
        http.cookies.clear()
    return tokens


def hf_meta(features):
    return {b"huggingface": json.dumps({"info": {"features": features}}).encode()}


def png_bytes(color, size=48):
    img = Image.new("RGB", (size, size), color)
    px = img.load()
    for i in range(size):  # a visible pattern so a thumbnail is recognisable
        px[i, i] = (255, 255, 255)
        px[size - 1 - i, i] = (0, 0, 0)
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()


def jpg_bytes(color, size=128):
    img = Image.new("RGB", (size, size), color)
    px = img.load()
    for i in range(size):
        px[i, size // 2] = (255, 255, 255)
        px[size // 2, i] = (0, 0, 0)
    b = io.BytesIO(); img.save(b, "JPEG", quality=85); return b.getvalue()


def text(n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


truth = {}
tokens = users()
api = HfApi(endpoint=BASE, token=tokens["proto"])


def commit(repo, ops, msg, private=False, revision=None):
    api.create_repo(repo, repo_type="dataset", private=private, exist_ok=True)
    return api.create_commit(repo_id=repo, repo_type="dataset", operations=ops, commit_message=msg, revision=revision).oid


def parquet_bytes(table, row_group_size):
    b = io.BytesIO(); pq.write_table(table, b, row_group_size=row_group_size, compression="zstd"); return b.getvalue()


# ------------------------------------------------------------------ 1. proto/reviews: multi-shard tabular with README YAML
features = {
    "id": {"dtype": "int64", "_type": "Value"},
    "text": {"dtype": "string", "_type": "Value"},
    "label": {"names": LABELS, "_type": "ClassLabel"},
    "score": {"dtype": "float64", "_type": "Value"},
    "tags": {"feature": {"dtype": "string", "_type": "Value"}, "_type": "List"},
    "created_at": {"dtype": "timestamp[us]", "_type": "Value"},
}


def reviews_table(start, n):
    ids = np.arange(start, start + n, dtype="int64")
    labels = np_rng.integers(0, 5, n)
    score = np.round(labels * 0.9 + np_rng.normal(0, 0.6, n), 3)
    created = (np.datetime64("2026-01-01T00:00:00", "us") + (np_rng.integers(0, 86400 * 270, n) * 1_000_000).astype("timedelta64[us]"))
    nulls = np_rng.random(n) < 0.03
    return pa.table(
        {
            "id": ids,
            "text": pa.array([text(rng.randint(6, 28)) if not nulls[i] else None for i in range(n)], pa.string()),
            "label": labels,
            "score": score,
            "tags": pa.array([[rng.choice(WORDS) for _ in range(rng.randint(0, 3))] for _ in range(n)], pa.list_(pa.string())),
            "created_at": created,
        }
    ).replace_schema_metadata(hf_meta(features))


shards = {
    "data/train-00000-of-00002.parquet": reviews_table(0, 20000),
    "data/train-00001-of-00002.parquet": reviews_table(20000, 20000),
    "data/test-00000-of-00001.parquet": reviews_table(40000, 8000),
}
readme = """---
license: mit
task_categories:
- text-classification
configs:
- config_name: default
  data_files:
  - split: train
    path: data/train-*.parquet
  - split: test
    path: data/test-*.parquet
---
# reviews

Synthetic reviews: 40 000 train rows in two shards, 8 000 test rows.
"""
ops = [CommitOperationAdd("README.md", readme.encode())]
for path, table in shards.items():
    ops.append(CommitOperationAdd(path, parquet_bytes(table, 5000)))
commit("proto/reviews", ops, "Add reviews")
all_train = pa.concat_tables([shards["data/train-00000-of-00002.parquet"], shards["data/train-00001-of-00002.parquet"]])
labels_hist = np.bincount(all_train["label"].to_numpy(), minlength=5).tolist()
truth["reviews"] = {
    "train_rows": all_train.num_rows, "test_rows": 8000, "label_hist_train": labels_hist,
    "text_nulls_train": all_train["text"].null_count, "score_min_train": float(np.min(all_train["score"].to_numpy())),
    "score_max_train": float(np.max(all_train["score"].to_numpy())), "row_group_size": 5000,
    "row_20000": {"id": 20000, "text": shards["data/train-00001-of-00002.parquet"]["text"][0].as_py()},
}

# ------------------------------------------------------------------ 2. proto/images-parquet: image column in HF style
colors = [(200, 60, 60), (60, 200, 60), (60, 60, 200), (200, 200, 60), (60, 200, 200), (200, 60, 200)]
n_img = 600
img_features = {"id": {"dtype": "int64", "_type": "Value"}, "caption": {"dtype": "string", "_type": "Value"}, "image": {"_type": "Image"}}
img_table = pa.table(
    {
        "id": np.arange(n_img, dtype="int64"),
        "caption": [f"sample {i}: {text(5)}" for i in range(n_img)],
        "image": pa.array([{"bytes": png_bytes(colors[i % 6]), "path": f"img_{i:04d}.png"} for i in range(n_img)],
                          pa.struct([("bytes", pa.binary()), ("path", pa.string())])),
    }
).replace_schema_metadata(hf_meta(img_features))
commit("proto/images-parquet", [CommitOperationAdd("data/train-00000-of-00001.parquet", parquet_bytes(img_table, 100))], "Add images")
truth["images"] = {"rows": n_img}

# ------------------------------------------------------------------ 3. proto/mixed-formats: jsonl + csv, split names from file names only
rows_jsonl = [{"id": i, "text": text(8), "label": rng.randint(0, 2), "meta": {"len": rng.randint(1, 9), "src": rng.choice(["a", "b"])}} for i in range(6000)]
jsonl = ("\n".join(json.dumps(r) for r in rows_jsonl) + "\n").encode()
csv_lines = ["id,name,value"] + [f"{i},item-{i},{round(rng.random()*100,3)}" for i in range(3000)]
commit("proto/mixed-formats", [CommitOperationAdd("train.jsonl", jsonl), CommitOperationAdd("eval.csv", ("\n".join(csv_lines) + "\n").encode()),
                               CommitOperationAdd("README.md", b"# mixed formats\n")], "Add jsonl and csv")
truth["mixed"] = {"jsonl_rows": 6000, "csv_rows": 3000}

# ------------------------------------------------------------------ 4. proto/webds: WebDataset shards (jpg + json + txt per sample)
samples_per_shard, n_shards = 200, 3
ops = []
for s in range(n_shards):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for k in range(samples_per_shard):
            key = f"{s:02d}{k:05d}"
            color = colors[(s * 7 + k) % 6]
            for ext, data in (("jpg", jpg_bytes(color)), ("json", json.dumps({"id": s * samples_per_shard + k, "label": (s + k) % 4, "caption": text(5)}).encode()), ("txt", text(6).encode())):
                info = tarfile.TarInfo(f"{key}.{ext}"); info.size = len(data); tf.addfile(info, io.BytesIO(data))
    ops.append(CommitOperationAdd(f"shards/shard-{s:04d}.tar", buf.getvalue()))
commit("proto/webds", ops + [CommitOperationAdd("README.md", b"# webdataset\n")], "Add WebDataset shards")
truth["webds"] = {"shards": n_shards, "samples_per_shard": samples_per_shard, "members_per_sample": 3}

# ------------------------------------------------------------------ 5. proto/versions: two commits of one table
def table_v(ids, bump=0, flag=False):
    cols = {"id": np.array(ids, dtype="int64"), "name": [f"row-{i}" for i in ids], "value": np.array([i * 1.5 + (bump if i % 30 == 0 else 0) for i in ids], dtype="float64")}
    if flag:
        cols["flag"] = [i % 2 == 0 for i in ids]
    return pa.table(cols)


v1_ids = list(range(3000))
c1 = commit("proto/versions", [CommitOperationAdd("table.parquet", parquet_bytes(table_v(v1_ids), 1000))], "v1: 3000 rows")
v2_ids = [i for i in range(3000) if not (2900 <= i < 2950)] + list(range(3000, 3300))   # 50 removed, 300 added
c2 = commit("proto/versions", [CommitOperationAdd("table.parquet", parquet_bytes(table_v(v2_ids, bump=10, flag=True), 1000))], "v2: +300 rows, -50 rows, value changed for 1 in 30 ids, new column flag")
changed = len([i for i in v1_ids if i % 30 == 0 and i in set(v2_ids)])
truth["versions"] = {"v1": c1, "v2": c2, "v1_rows": 3000, "v2_rows": len(v2_ids), "added": 300, "removed": 50, "changed": changed, "new_column": "flag"}

# ------------------------------------------------------------------ 6. proto/private-data: private, owner proto
commit("proto/private-data", [CommitOperationAdd("data.parquet", parquet_bytes(table_v(list(range(500))), 250))], "Add private table", private=True)

json.dump(truth, open(os.path.join(OUT, "truth.json"), "w"), indent=1)
fd = os.open(os.path.join(OUT, "tokens.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, json.dumps(tokens).encode()); os.close(fd)
print("seeded:", json.dumps({k: (v if k != "versions" else {kk: vv for kk, vv in v.items() if not kk.startswith("v") or kk in ("v1_rows", "v2_rows")}) for k, v in truth.items()})[:400])
