# Repository import: relay spikes

Throw-away measurements for the design in issue #17. They stream one file from a Hugging Face repository into an S3-compatible bucket
(a local MinIO in the runs below) with **no disk and bounded memory**. Nothing here is production code.

Credentials come from the environment: `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (and optionally `S3_BUCKET`). Objects are written under `spike/`; `clean.py` removes them.

| Script | What it tests |
|---|---|
| `relay_buffered.py <repo> <path> <part_mib> <workers> [fail_after_parts]` | Parallel Range reads, each part buffered in memory, uploaded with boto3. **Resume**: the checkpoint is the multipart upload itself (`ListParts`). Verification afterwards by streaming the stored object. |
| `relay_stream.py <repo> <path> <part_mib>` | **Zero-buffer** relay: the source response is piped into a presigned `UploadPart` PUT (known `Content-Length`), sha256 computed in flight. |

## Measured (2026-10-23, one machine, public `openai-community/gpt2`)

| Run | File | Result |
|---|---|---|
| buffered, killed after 15 of 33 parts | `model.safetensors`, 548 MB | 15 parts stayed in the bucket |
| buffered, resumed | same | uploaded only the remaining 18 parts; stored 548,105,171 bytes; sha256 equals the LFS oid; peak RSS 262 MiB (4 workers x 16 MiB parts) |
| zero-buffer | `64-8bits.tflite`, 125 MB | sha256 (in flight) equals the LFS oid; stored 125,162,496 bytes; **peak RSS 63 MiB against a 56 MiB baseline** after imports |

Not measured: throughput (this machine reaches Hugging Face only through a local proxy, about 2 MB/s, so the numbers say nothing about the relay),
several files at once, files above 548 MB, real Cloudflare R2 (the runs used MinIO), a Cloudflare Worker.
No file is written by the scripts; that is by construction, syscalls were not traced.

## UI mockup (`mockup/`)

`import-mockup-web.patch` adds an **Import a repository** page (`/import`: form, preview, progress), an **Import to local** button on repositories served from another hub, and a link on the New repository page,
to the CheeseCave web app. It touches `RepoViewer.vue` like the Data Explorer patch of #118 does, so apply it **after** that one.
`mockup/test_import_mockup.py` drives the screens with Playwright against a **faked** API (the import API does not exist yet), checks what every state shows,
and takes the numbered screenshots. All data is synthetic.
