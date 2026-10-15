---
title: HuggingFace-Compatible API
description: Endpoints compatible with huggingface_hub library
icon: i-carbon-cloud-app
---

# HuggingFace-Compatible API

KohakuHub implements HuggingFace Hub API for compatibility with `huggingface_hub`, `transformers`, and `diffusers` libraries.

---

## Repository Info

### Get Repository Information

**Pattern:** `GET /api/{repo_type}s/{namespace}/{name}`

**Examples:**
- `GET /api/models/username/bert-base`
- `GET /api/datasets/org/imagenet`
- `GET /api/spaces/user/gradio-app`

**Query Parameters:**
- `expand` (repeatable, also spelled `expand[]`): answer only these properties (plus `_id` and `id`); the names are the Hub's
- `blobs`: size, blob id and LFS data per file in `siblings`

**Request Headers:**
- `Authorization: Bearer {token}` (optional, for private repos)

**Response:**
```json
{
  "_id": "42",
  "id": "username/bert-base",
  "modelId": "username/bert-base",
  "author": "username",
  "sha": "abc123def456...",
  "lastModified": "2025-01-20T10:15:32.123Z",
  "createdAt": "2025-01-15T08:00:00.000Z",
  "private": false,
  "disabled": false,
  "gated": false,
  "downloads": 150,
  "likes": 25,
  "tags": ["transformers", "fill-mask", "en", "license:mit", "region:us"],
  "pipeline_tag": "fill-mask",
  "library_name": "transformers",
  "cardData": {"license": "mit", "language": "en", "pipeline_tag": "fill-mask", "library_name": "transformers"},
  "siblings": [
    {"rfilename": "config.json"},
    {"rfilename": "model.safetensors"}
  ],
  "storage": {
    "quota_bytes": 107374182400,
    "used_bytes": 5000001024,
    "available_bytes": 102374181376,
    "percentage_used": 4.66,
    "effective_quota_bytes": 107374182400,
    "is_inheriting": false
  }
}
```

**Notes:**
- `siblings`: the name of every file by default, as on the Hub. `blobs=true` (`files_metadata=True`) adds `blobId`, `size` and `lfs`; `expand=...` returns only the named properties. See [Get Repository Info](./repositories.md#get-repository-info).
- `storage`: Only included for authenticated users
- `sha`: Latest commit hash on main branch
- **The README card.** `cardData` is the README front matter as the Hub returns it (keys as written; only what JSON can carry: non-string keys become strings, dates ISO strings, `tags` always a list of strings, `ignore_metadata_errors` dropped, every `model-index` metric `verified: false` as the Hub forces it). `tags`, `pipeline_tag`, `library_name` (models) and `sdk` (spaces) are what the card declares; `tags` follow the Hub's order and are `[]` for a repository whose card cannot be read now. A repository without a card has no `cardData` and no `model-index`. The card is read from the head commit, so a README pushed shows at once.
- Per type: datasets have `paperswithcode_id` (from the card) and no `modelId`, `pipeline_tag` or `library_name`; spaces have `sdk` and no download counts.
- **What the files say (models).** When the card does not say, `library_name`, `pipeline_tag`, the framework tags (`pytorch`, `safetensors`, `tf`, `jax`, `onnx`, `gguf`, ...), the `model_type` tag, `diffusers:<PipelineClass>` and `config` come from the files at the head commit: `model_index.json`, `modules.json`, `adapter_config.json`, `config.json` with root weights, and the head class in `config.json` (`...ForMaskedLM`, `...ForCausalLM` and a few more) and a lone task named in the card's tags (`translation`: translation models are `ForCausalLM` too). `tags` follow the Hub's order (library, frameworks, task, then the card's own). `config` has `architectures`, `model_type`, `auto_map` and the special tokens of `tokenizer_config`. **A value is returned only when it is right**: a rule that was wrong for one repository in twenty is not a rule, so `library_name` / `pipeline_tag` stay `null` where the files do not settle it (a `config.json` that other libraries also write, `...ForSequenceClassification`, which audio models share, a card that names two libraries). The rules were checked against what the real Hub answered for 542 repositories, none wrong. A task the Hub merged into another (`text2text-generation`, `conversational`) is answered as `text-generation` in `pipeline_tag`, its tag keeps the words of the card. Repositories of more than 3000 files are described by their card alone.
- **`safetensors` (models).** `{"parameters": {dtype: count}, "total": n}` summed from the headers of the weights in the repository root (`model.safetensors`, or the complete set `model-0000i-of-0000N.safetensors`), as the Hub counts them (a tensor without dimensions is not a parameter). It is computed in the background after the index of a commit, so it is `null` for a moment after a push and not part of the wait of an info request; it stays while the head does not move. **Only what is right is given**: `null` for a quantized model (the Hub counts what is unpacked, and the stored tensors say less), for two sets of weights in one repository, for an incomplete or unreadable set (and above 300 files), and for diffusers pipelines (the Hub sums a component of its own choosing). Checked against what the real Hub answers for 172 repositories: every summary given equals it, none differ. `get_safetensors_metadata` is unchanged and agrees with it.
- Not answered yet (the Hub derives them from the files or computes them in its own services): `transformersInfo` (92% exact on a sample: the Hub also looks at custom code and processor files), `widgetData`, the `text-generation-inference` / `endpoints_compatible` / `deploy:*` tags, a dataset's `size_categories` / `modality:*` / `format:*` tags.
- Compatible with `transformers.from_pretrained()` and `diffusers`

---

## Repository Listing

### List Repositories by Type

**Pattern:** `GET /api/{repo_type}s`

**Examples:**
- `GET /api/models?author=username&limit=50&sort=recent`
- `GET /api/datasets?sort=trending&limit=20`

**Query Parameters:**
- `author` (optional): Filter by namespace
- `limit` (optional): Items per response (default and maximum: 1000, like the Hub; a larger value is cut to 1000)
- `sort` (optional): `trending` | `recent` | `updated` | `likes` | `downloads` (default: `trending`). The Hub's names work too: `trendingScore`, `createdAt`, `lastModified` (and their snake_case forms)
- `direction` (optional): `-1` (descending, default) or `1`
- `search` (optional): case-insensitive substring of the repository id
- `filter` (repeatable), `pipeline_tag`, `library`, `language`, `task_categories`, `size_categories` (optional): match the indexed facets of the repository; a tag with a prefix this hub does not index (`base_model:`, ...) matches nothing
- `fallback` (optional): Enable external sources (default: true)
- `partial` (optional): `ok` to skip an external source that cannot answer instead of failing (see below)
- `cardData` / `full` (optional): add `cardData` to every item
- `cursor`: opaque, only ever taken from a `Link` header

Unknown parameters are ignored.

**Paging.** The body is a bare array. When more follows, the response carries
`Link: <https://hub.example/api/models?...&cursor=...>; rel="next"` (RFC 8288, the format the Hub
uses); the last page has no `Link`. `huggingface_hub` follows it by itself, so `list_models()` with
no `limit` reads everything. The list is one ordered stream: this hub's repositories first, then
each fallback source in priority order. A `cursor` is signed, bound to the query that made it
(another query's cursor is a 400) and carries no URL.

**When an external source fails.** It is retried; if it still cannot answer, the response is
`503` with `Retry-After` (clients retry on it), because a list that silently lacks a source is
incomplete. With `partial=ok` the source is skipped and the response says so in
`X-Cheesecave-Incomplete: <source names>`. Both headers are exposed to browsers by CORS.

**Response:**
```json
[
  {
    "id": "username/model-name",
    "author": "username",
    "private": false,
    "sha": "commit_hash",
    "lastModified": "2025-01-20T10:15:32Z",
    "createdAt": "2025-01-15T08:00:00Z",
    "downloads": 150,
    "likes": 25,
    "gated": false,
    "tags": []
  }
]
```

---

### List User Repositories

**Pattern:** `GET /api/users/{username}/repos`

**Query Parameters:**
- `limit` (optional): Max per type (default: 100)
- `sort` (optional): `recent` | `likes` | `downloads`

**Response:**
```json
{
  "models": [...],
  "datasets": [...],
  "spaces": [...]
}
```

---

### Validate a README card

`POST /api/validate-yaml` with `{"content": "<README text>", "repoType": "model"}` (`repo_type` is accepted too), as `huggingface_hub` sends it before it uploads a README: `200 {"errors": [], "warnings": [...]}`, or `400 {"errors": [{"message", "path"}]}` when the metadata cannot be read (invalid YAML, or not a mapping). A README without metadata is a warning ("empty or missing yaml metadata in repo card"). The Hub's own lists (licenses, languages, tasks) are not checked. `valid` is the older answer of this route and is kept. The client refuses to upload a README this route refuses.

## Access Preflight and Tag Vocabularies

- `GET /api/{models,datasets,spaces}/{namespace}/{name}/auth-check` (`HfApi.auth_check`): `200` when the caller can read the repository; otherwise `404` with `X-Error-Code: RepoNotFound`, the same answer for a private and a missing repository (the client raises `RepositoryNotFoundError`). A repository only a fallback source has is answered by that source, like the info route.
- `GET /api/models-tags-by-type`, `GET /api/datasets-tags-by-type` (`get_model_tags`, `get_dataset_tags`): the tags **in use on this hub** among what the caller may read, grouped like the Hub's (`pipeline_tag`, `library`, `language`, `license`, `other`, ... for models; `task_categories`, `size_categories`, `language`, `license`, ... for datasets), each entry `{id, label, type}`. The Hub's lists are global and curated; this one is what the site indexed.

## File Operations

### File Download/Resolution

**Pattern:** `GET /{repo_type}s/{namespace}/{name}/resolve/{revision}/{path}`

**Examples:**
- `GET /models/username/bert/resolve/main/config.json`
- `GET /datasets/org/data/resolve/v1.0/train.csv`

**Path Parameters:**
- `repo_type`: models | datasets | spaces
- `namespace`: User or organization name
- `name`: Repository name
- `revision`: Branch name or commit hash
- `path`: File path in repository

**Response:**
- `302 Found` - Redirect to S3 presigned URL
- `404 Not Found` - File doesn't exist

**Response Headers:**
- `X-Repo-Commit`: Commit hash
- `X-Linked-Etag`: File SHA256
- `X-Linked-Size`: File size in bytes
- `ETag`: File SHA256
- `Content-Disposition`: attachment; filename="..."

**Notes:**
- No body returned (302 redirect only)
- Client follows redirect to download from S3
- Presigned URL valid for 24 hours
- Download tracking happens in background

---

### File Metadata (HEAD)

**Pattern:** `HEAD /{repo_type}s/{namespace}/{name}/resolve/{revision}/{path}`

**Response:**
- `200 OK` with headers (no body)
- Same headers as GET endpoint

**Use case:** Check file exists and get metadata without downloading

---

### Preupload Check

**Pattern:** `POST /{repo_type}s/{namespace}/{name}/preupload/{revision}`

**Purpose:** Check if files should use LFS and if they already exist (deduplication)

**Request Body:**
```json
{
  "files": [
    {
      "path": "model.safetensors",
      "size": 5000000000,
      "sha256": "abc123...",  // Optional but recommended
      "sample": "base64..."    // Optional, for small files
    },
    {
      "path": "config.json",
      "size": 512
    }
  ]
}
```

**Response:**
```json
{
  "files": [
    {
      "path": "model.safetensors",
      "uploadMode": "lfs",
      "shouldIgnore": false
    },
    {
      "path": "config.json",
      "uploadMode": "regular",
      "shouldIgnore": true
    }
  ]
}
```

**Field Explanations:**
- `uploadMode`:
  - `lfs`: Use Git LFS (file > threshold OR matches suffix rules)
  - `regular`: Use regular commit (small file)
- `shouldIgnore`:
  - `true`: File with same content already exists (skip upload)
  - `false`: File is new or changed (upload required)

**Deduplication Logic:**
1. If `sha256` provided: Compare with existing file SHA256
2. If `sample` provided (small files): Compare sample content
3. If neither: Always upload

---

## Revision Info

### Get Revision Details

**Pattern:** `GET /{repo_type}s/{namespace}/{name}/revision/{revision}`

**Examples:**
- `GET /models/user/bert/revision/main`
- `GET /datasets/org/data/revision/v2.0`

**Query Parameters:**
- `expand` (optional): Fields to expand (reserved for future use)

**Response:**
```json
{
  "id": "username/repo",
  "author": "username",
  "sha": "commit_hash",
  "lastModified": "2025-01-20T10:15:32Z",
  "createdAt": "2025-01-15T08:00:00Z",
  "private": false,
  "downloads": 150,
  "likes": 25,
  "gated": false,
  "files": [],
  "type": "model",
  "revision": "main",
  "commit": {
    "oid": "commit_hash",
    "date": 1737360932
  },
  "xetEnabled": false
}
```

**Notes:**
- `files` is empty (use `/tree` endpoint for file list)
- `commit.date`: Unix timestamp

---

## Privacy & Permissions

### Access Control Rules

**Public repositories:**
- ✅ Anyone can read
- ⚠️ Only owner/org members can write

**Private repositories:**
- ⚠️ Only owner/org members can read
- ⚠️ Only owner/org members can write

**Anonymous requests:**
- ✅ Can access public repositories
- ❌ Cannot access private repositories

**Authentication:**
- Session cookie: `session_id` (for browser)
- Bearer token: `Authorization: Bearer {token}` (for API/CLI)

---

## Error Responses

**Standard HuggingFace error format:**

```json
{
  "error": "Error message",
  "requestId": "optional-request-id"
}
```

**Common status codes:**
- `200 OK` - Success
- `302 Found` - Redirect to S3 (download endpoints)
- `400 Bad Request` - Invalid input
- `401 Unauthorized` - Not authenticated
- `403 Forbidden` - No permission
- `404 Not Found` - Repository or file not found
- `413 Payload Too Large` - Quota exceeded
- `500 Internal Server Error` - Server error

**Custom headers:**
- `X-Error-Code`: Machine-readable error code
- `X-Request-Id`: Request tracking ID (if available)

---

## Compatibility Notes

### Works with these libraries:

✅ **`huggingface_hub`**
```python
from huggingface_hub import hf_hub_download

file = hf_hub_download(
    repo_id="username/model",
    filename="config.json",
    repo_type="model",
    endpoint="http://localhost:28080"
)
```

✅ **`transformers`**
```python
from transformers import AutoModel

model = AutoModel.from_pretrained(
    "username/bert-base",
    trust_remote_code=True,
    use_auth_token="your_token"
)
# Set HF_ENDPOINT=http://localhost:28080
```

✅ **`diffusers`**
```python
from diffusers import StableDiffusionPipeline

pipe = StableDiffusionPipeline.from_pretrained(
    "username/sd-model",
    use_auth_token="your_token"
)
# Set HF_ENDPOINT=http://localhost:28080
```

### `huggingface_hub` 2.0 and later: turn Xet off for uploads

`huggingface_hub` 2.0 and later install `hf_xet` and send every LFS-sized file (a file above the repository's LFS
threshold) through Hugging Face's Xet protocol first. KohakuHub stores files as Git LFS objects and does not implement
Xet, so that upload fails before a byte is sent:

```
ConnectionError: Network error: Request error: HTTP status client error (404 Not Found),
domain: http://<host>/api/models/<namespace>/<name>/xet-write-token/main
```

(a larger upload says `RuntimeError: Previous task error: Network error: ...` with the same `xet-write-token` URL).
The switch is on the client side and no server answer can turn it off, so set `HF_HUB_DISABLE_XET=1` next to
`HF_ENDPOINT`; the client then uses the ordinary LFS upload:

```bash
export HF_ENDPOINT=http://localhost:28080
export HF_HUB_DISABLE_XET=1
```

In Python, set it before `huggingface_hub` is first imported:

```python
import os
os.environ["HF_ENDPOINT"] = "http://localhost:28080"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from huggingface_hub import HfApi
```

In a container or a CI job, pass it in the environment (`docker run -e HF_HUB_DISABLE_XET=1`, `env:` in a workflow).

- **Affected:** uploads of LFS-sized files (`upload_file`, `upload_folder`, `create_commit`, `hf upload`) with
  `huggingface_hub` 2.0 and later (checked with 2.0.0 and 2.2.0). Small files, downloads, and `huggingface_hub` 1.x and
  older are not affected (checked with 1.6.0).
- **Checked:** with the variable set, 2.0.0 and 2.2.0 upload a 3 MiB file and a 20 MiB file (three parts) against a real
  stack.
- **Cost:** the 2.x client uploads the parts of an LFS file one after the other on one connection, so a large upload can
  be slower than Xet is on huggingface.co, where the client sends many parts in parallel.
- **Not planned:** Xet needs a service in front of the object store that receives every uploaded byte. KohakuHub
  uploads go to object storage directly through presigned URLs, and moving them through the application server would
  send each byte through its network about four times and keep memory busy for the length of every upload. See
  [#71](https://github.com/cheesecave/cheesecave-backend/issues/71) for the measurements and the options.

### Differences from HuggingFace Hub:

**Not implemented:**
- Xet storage (see above: set `HF_HUB_DISABLE_XET=1`)
- Gated repositories (always `gated: false`; `update_repo_settings(gated="auto"|"manual")` answers `501` instead of pretending, `gated=False` is accepted)
- Pipeline tags (always `null`)
- Model cards parsing (use README.md YAML frontmatter)
- Discussions/Community features

**Refused with a `501` that says why:** a feature KohakuHub does not provide answers `501 Not Implemented` with
`X-Error-Code: NotImplemented` and an `X-Error-Message` that says what to do instead, so `huggingface_hub` raises an error
you can read (a `404` would look like a bug). The surfaces:

| Feature | Routes | What the message says |
| --- | --- | --- |
| Discussions and pull requests, `create_pr` | `/api/{type}s/{repo}/discussions...` | use an external issue tracker |
| Collections | `/api/collections...` | not implemented |
| Webhooks | `/api/settings/webhooks...` | not implemented |
| Space runtime, secrets, variables, hardware, pause, restart | `/api/spaces/{repo}/runtime`, `.../secrets`, ... | configure your deployment directly |
| Space logs, Space templates, the hardware list | `/api/spaces/{repo}/logs/{run\|build}`, `/api/spaces/templates`, `/api/spaces/hardware` | same |
| Papers | `/api/papers/...`, `/api/daily_papers` | there is no paper catalogue |
| Jobs | `/api/jobs/...` | KohakuHub does not run compute |
| Storage buckets, kernels | `/api/buckets/...`, `/api/kernels/...` | keep the files in a repository |
| Gated-access requests | `/api/{type}s/{repo}/user-access-request/...` | a repository is public or private here |

`huggingface_hub` 2.0 and later show the message in the exception. Three older releases (0.36, 1.0 and 1.6) build `list_jobs`
on a bare `raise_for_status`, so there the error carries the `501` status and the URL but not the sentence.

**Extensions:**
- Storage quota information (KohakuHub-specific)
- Organization support with roles
- Per-repository LFS settings
- Fallback to external sources

---

## Rate Limiting

**No rate limits on HuggingFace-compatible endpoints** (by design for library compatibility)

**Recommended:** Use reverse proxy (nginx) for rate limiting in production

---

## Next: See other API docs

- [Authentication API](./authentication.md)
- [Admin API](./admin.md)
- [Git/LFS API](./git-lfs.md) (TODO)
- [File Upload API](./file-upload.md) (TODO)
