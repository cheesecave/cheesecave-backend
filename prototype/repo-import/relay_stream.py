"""Spike 2: zero-buffer relay. The source response is piped straight into a presigned UploadPart PUT (Content-Length known from the Range),
sha256 is computed in flight, nothing is held but a 64 KiB chunk. The hub's only part is minting the presigned URLs.

usage: spike_stream.py <repo_id> <path> <part_mib>
"""
import hashlib
import json
import resource
import sys
import time

import os

import boto3
import httpx
from botocore.config import Config

repo, path, part_mib = sys.argv[1], sys.argv[2], int(sys.argv[3])
PART, CHUNK = part_mib * 1024 * 1024, 64 * 1024
BUCKET, KEY = os.environ.get("S3_BUCKET", "hub-storage"), f"spike/stream__{repo.replace('/', '__')}__{path.replace('/', '__')}"
rss = lambda: round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
                  region_name="us-east-1", config=Config(signature_version="s3v4"))
http = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(180, connect=30), trust_env=True)
baseline = rss()
URL = f"https://huggingface.co/{repo}/resolve/main/{path}"
info = next(e for e in http.get(f"https://huggingface.co/api/models/{repo}/tree/main").json() if e["path"] == path)
size, oid = info["size"], info["lfs"]["oid"]
nparts = -(-size // PART)
print(f"{path}: {size / 1e6:.0f} MB, {nparts} parts of {part_mib} MiB; RSS after imports {baseline} MiB", flush=True)

upload_id = s3.create_multipart_upload(Bucket=BUCKET, Key=KEY)["UploadId"]
digest, parts, t0 = hashlib.sha256(), [], time.time()
for n in range(1, nparts + 1):
    lo, hi = (n - 1) * PART, min(n * PART, size) - 1
    # the hub would hand this URL to the runner; here we mint it ourselves
    put_url = s3.generate_presigned_url("upload_part", Params={"Bucket": BUCKET, "Key": KEY, "UploadId": upload_id, "PartNumber": n}, ExpiresIn=3600)
    with http.stream("GET", URL, headers={"Range": f"bytes={lo}-{hi}"}) as src:
        def body():
            for chunk in src.iter_bytes(CHUNK):
                digest.update(chunk)
                yield chunk
        r = httpx.put(put_url, content=body(), headers={"Content-Length": str(hi - lo + 1)}, timeout=httpx.Timeout(180, connect=30), trust_env=True)
    r.raise_for_status()
    parts.append({"PartNumber": n, "ETag": r.headers["ETag"]})
t_transfer = time.time() - t0
ok = digest.hexdigest() == oid
if ok:
    s3.complete_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId=upload_id, MultipartUpload={"Parts": parts})
else:
    s3.abort_multipart_upload(Bucket=BUCKET, Key=KEY, UploadId=upload_id)
print(json.dumps({"sha256_matches_upstream_in_flight": ok, "stored_bytes": s3.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"] if ok else None,
                  "transfer_s": round(t_transfer, 1), "MB_per_s": round(size / 1e6 / t_transfer, 1), "peak_RSS_MiB": rss(), "RSS_baseline_MiB": baseline}), flush=True)
