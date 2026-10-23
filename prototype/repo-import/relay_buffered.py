"""Spike: stream one Hugging Face file straight into S3 (MinIO) as a multipart upload: no disk, bounded memory, parallel Range reads,
resumable (the checkpoint is the multipart upload itself: ListParts), verified afterwards by streaming sha256 from the bucket
(what the hub's existing LFS verify does), before anything is published.

usage: spike_relay.py <repo_id> <path> <part_mib> <workers> [fail_after_parts]    (run again without fail_after_parts to resume)
"""
import hashlib
import json
import os
import resource
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import httpx
from botocore.config import Config

repo, path, part_mib, workers = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
fail_after = int(sys.argv[5]) if len(sys.argv) > 5 else None
PART = part_mib * 1024 * 1024
BUCKET, KEY = os.environ.get("S3_BUCKET", "hub-storage"), f"spike/{repo.replace('/', '__')}__{path.replace('/', '__')}"
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
                  region_name="us-east-1", config=Config(signature_version="s3v4", max_pool_connections=workers * 2 + 4))
http = httpx.Client(follow_redirects=True, timeout=180, trust_env=True, limits=httpx.Limits(max_connections=workers * 2))
URL = f"https://huggingface.co/{repo}/resolve/main/{path}"
rss = lambda: round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)

t_start = time.time()
info = next(e for e in http.get(f"https://huggingface.co/api/models/{repo}/tree/main").json() if e["path"] == path)
size, oid = info["size"], info["lfs"]["oid"]
nparts = -(-size // PART)
print(f"source {path}: {size / 1e6:.0f} MB, sha256 {oid[:12]}.., {nparts} parts of {part_mib} MiB, {workers} workers", flush=True)

uploads = [u for u in s3.list_multipart_uploads(Bucket=BUCKET, Prefix=KEY).get("Uploads", []) if u["Key"] == KEY]
if uploads:
    upload_id, resumed = uploads[0]["UploadId"], True
    parts = {p["PartNumber"]: {"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in s3.list_parts(Bucket=BUCKET, Key=KEY, UploadId=upload_id).get("Parts", [])}
else:
    upload_id, resumed, parts = s3.create_multipart_upload(Bucket=BUCKET, Key=KEY)["UploadId"], False, {}
todo = [n for n in range(1, nparts + 1) if n not in parts]
print(f"{'RESUME' if resumed else 'START'}: {len(parts)} parts already in the bucket, {len(todo)} to go", flush=True)

lock = threading.Lock()
done_now, inflight, peak_inflight = [0], [0], [0]


def run_part(n: int):
    if fail_after is not None and done_now[0] >= fail_after:
        raise SystemExit(f"injected failure at part {n}")
    lo, hi = (n - 1) * PART, min(n * PART, size) - 1
    with lock:
        inflight[0] += 1
        peak_inflight[0] = max(peak_inflight[0], inflight[0])
    buf = http.get(URL, headers={"Range": f"bytes={lo}-{hi}"}).content
    assert len(buf) == hi - lo + 1, (n, len(buf))
    r = s3.upload_part(Bucket=BUCKET, Key=KEY, UploadId=upload_id, PartNumber=n, Body=buf)
    with lock:
        parts[n] = {"PartNumber": n, "ETag": r["ETag"]}
        done_now[0] += 1
        inflight[0] -= 1


t0 = time.time()
try:
    with ThreadPoolExecutor(workers) as ex:
        for f in [ex.submit(run_part, n) for n in todo]:
            f.result()
except (SystemExit, Exception) as e:
    print(f"STOPPED ({e}); parts in the bucket: {len(s3.list_parts(Bucket=BUCKET, Key=KEY, UploadId=upload_id).get('Parts', []))}; peak RSS {rss()} MiB", flush=True)
    os._exit(3)
t_transfer = time.time() - t0

s3.complete_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId=upload_id, MultipartUpload={"Parts": [parts[n] for n in sorted(parts)]})
t1 = time.time()
digest = hashlib.sha256()                                    # what the hub's LFS verify does: stream the stored object, 8 MiB at a time
body = s3.get_object(Bucket=BUCKET, Key=KEY)["Body"]
for chunk in body.iter_chunks(8 * 1024 * 1024):
    digest.update(chunk)
t_verify = time.time() - t1
ok = digest.hexdigest() == oid
if not ok:
    s3.delete_object(Bucket=BUCKET, Key=KEY)
print(json.dumps({"sha256_matches_upstream": ok, "stored_bytes": s3.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"] if ok else None,
                  "transfer_s": round(t_transfer, 1), "transfer_MB_per_s": round((size - (len(parts) - done_now[0]) * PART) / 1e6 / max(t_transfer, 0.01), 1),
                  "verify_read_s": round(t_verify, 1), "verify_MB_per_s": round(size / 1e6 / t_verify), "peak_RSS_MiB": rss(),
                  "peak_parts_in_flight": peak_inflight[0], "resumed": resumed, "parts_uploaded_this_run": done_now[0], "total_s": round(time.time() - t_start, 1)}), flush=True)
