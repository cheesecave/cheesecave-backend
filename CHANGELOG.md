# Changelog

All notable changes to KohakuHub are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`list_lfs_files` and `permanently_delete_lfs_files`** ([#66](https://github.com/cheesecave/cheesecave-backend/issues/66)).
  Both answered `404`. `GET /api/{type}s/{repo}/lfs-files` lists the LFS objects a repository stores (the Hub's
  `LFSFileInfo` shape, newest first, `Link` paging); `POST .../lfs-files/batch` deletes objects for good, all or none:
  an object a branch head links, an active file has, was uploaded in the last 24 hours or another repository's history
  keeps is refused with `409` and the reason (the decision is the garbage collection's, with the repository's own history
  left out of it); what goes leaves the bucket, its commits stay and are reported as missing the file, and the quota drops.
  Rewriting history is not supported (`501`), and `huggingface_hub` sends `rewriteHistory: true` by default, so call
  `permanently_delete_lfs_files(..., rewrite_history=False)`. See `docs/api/git-lfs.md`.

### Fixed

- **One form of a commit id in every response: the Hub's 40 hex** ([#12](https://github.com/cheesecave/cheesecave-backend/issues/12)).
  Repository info said `sha` in 40 hex while `revision/main`, the commit list, `X-Repo-Commit` and the rest said the
  64 hex LakeFS has. `huggingface_hub` accepts a `parent_commit` of at most 40 hex (every other source of a sha made
  `create_commit(parent_commit=...)` raise before it sent anything, a chain of commits included) and keeps its cache in a
  folder named by the sha it is told: `snapshot_download(revision=model_info().sha)` returned a folder that did not exist
  (2.0 and later) and diffusers' `from_pretrained` failed. Every place that says a commit now says the same form:
  `sha`, `commit.oid`, `id`, `oid`, `parents`, `commitOid`, `commitUrl`, `new_commit_id`, `targetCommit`, `lastCommit.id`,
  the `X-Repo-Commit` header, the workspace feed, and the ids in a commit's metadata. The form is set by
  `commit_id_format` (`KOHAKU_HUB_COMMIT_ID_FORMAT`): `hub` (the default, 40 hex) or `full` (the 64 hex LakeFS has), a
  transitional form for downstream systems that stored 64 hex and compare them with what a response says; `huggingface_hub`
  refuses `full` as a `parent_commit`. **A system that stores the ids a response says and compares them with ids stored
  earlier sees 40 hex where it saw 64: set `full` until it takes both.** Every request takes 64 hex, 40 hex or an
  abbreviation, in either form, so an id stored earlier still works. Nothing is stored differently (no migration).
- **The oid of an LFS file in a tree is the Hub's** (#12). `list_repo_tree` and `get_paths_info` said the sha256 as `oid`
  (`RepoFile.blob_id`); the Hub says the git blob id of the LFS pointer, as `model_info(files_metadata=True)` already did.
  The sha256 stays in `lfs.oid`.
- **A commit id in the Hub's form (40 hex) or abbreviated is read like the full one** ([#12](https://github.com/cheesecave/cheesecave-backend/issues/12)).
  `commit/{id}` and `commit/{id}/diff` looked the commit up in the database by the id as given, so a 40 hex id (what
  `huggingface_hub` holds) found nothing: the author became the LakeFS committer and `user_id`, `committed_at`,
  `description` were missing. They now use the commit's full id, answer it as `id`/`commit_id`, and answer a commit that
  does not exist with `404 RevisionNotFound` instead of `500`. The commit list routes
  (`commits/{branch}/operations`, `commits/unavailable-files`) take any form and answer under the id as it was asked,
  so an older page that holds 64 hex and a newer one that holds 40 both find their ids; the unavailable-files page
  returned nothing for a 40 hex id. `kohakuhub.utils.commit_id` holds the syntax of the forms. Nothing a client
  receives changes.
- **`resolve` tells a missing revision from a missing file** ([#58](https://github.com/cheesecave/cheesecave-backend/issues/58)).
  `GET`/`HEAD /{repo}/resolve/nope/<file>` answered `EntryNotFound`, so `hf_hub_download(revision="nope")`
  raised `EntryNotFoundError`; it is now `404 RevisionNotFound` (`RevisionNotFoundError`), and
  `EntryNotFound` only when the revision exists and the path does not. Every `stat_object` failure
  used to look like a missing file; a LakeFS failure is now a `500 ServerError` (retried by
  `huggingface_hub`). A revision LakeFS refuses as an id (`a..b`, `main~1`, a space) is a missing
  revision too, as "Invalid rev id" on the Hub, on `resolve`, `revision`, `tree` and the source of a
  new branch; it was a `500` where the error was not a plain 404.
- **`refs/heads/<branch>` is understood** (#58). The client sends the revision as
  `refs%2Fheads%2F<branch>`, which the server split at the slash. `resolve`, `revision` and `tree`
  now read it as the branch, like the Hub. A branch name with a slash of its own is still #67.
- **`If-None-Match` on `resolve`** ([#59](https://github.com/cheesecave/cheesecave-backend/issues/59)).
  A request naming the file's ETag gets `304` (GET and HEAD) instead of a redirect, with `ETag`,
  `X-Repo-Commit` and `X-Linked-Etag`, and is not counted as a download. The whole RFC 9110 form
  is accepted (list, `W/`, `*`), which is more than the Hub does.

- **Concurrent commits to one branch lost files** ([#56](https://github.com/cheesecave/cheesecave-backend/issues/56)).
  LakeFS commits whatever is staged on a branch, so two commit requests staging at once made
  one commit carry both requests' files. The other answered 500 or "no changes" although its
  files were committed, its file rows were undone, and with no row linking them the LFS
  collection deleted their objects a day later. A branch now takes one commit at a time, held
  in the database (`branch_commit_lock`, migration 034) from staging to the last record; any
  other request waits for its turn, like the Hub's concurrency queue, and is refused only after
  20 s with the Hub's own `429 maximum time in concurrency queue reached` and `Retry-After: 2`
  (huggingface_hub retries it with backoff), having staged nothing; the reason is also in
  `X-Error-Message`, which is where huggingface_hub reads it.
  Content that is already in the branch is no longer "undone": its rows and LFS links are
  kept, so uploading a lost file again repairs it.
- **`create_commit(parent_commit=...)` is honoured** ([#57](https://github.com/cheesecave/cheesecave-backend/issues/57)):
  a commit whose parent is no longer the branch head is refused with `412` ("A commit has
  happened since. Please refresh and try again."), as the Hub answers.
- **Upgrade safety net.** Migration 034 makes the next LFS reference reconciliation due, and
  the hourly expiry of uploads waits for it (whatever `lfs_auto_gc` says), so objects the old
  race left unlinked are linked before anything is collected. The reconciliation counts files
  that link an already deleted object (`linked_objects_collected`) and leaves them without
  row or link, so uploading them again works.

### Added

- **Account routes in the Hub's shapes** (#60, #64, #65). `list_user_followers`, `list_user_following`
  and `list_organization_followers` read a bare array with `Link` paging (they raised `TypeError`
  before); `whoami-v2` carries `avatarUrl` and the Hub's organization fields; user and organization
  overview, organization members, `auth_check`, `get_model_tags` and `get_dataset_tags` exist. The
  follower routes change shape (`{items, has_more, next_cursor}` -> a bare array): deploy the web
  client that reads both shapes first.
- **Hub-compatible repository lists** (#53, #54, #55). `GET /api/{models,datasets,spaces}` pages
  with `Link: <...>; rel="next"` (at most 1000 items per response), follows the Hub's sort names
  and `direction`, and supports `search`, `filter`, `pipeline_tag`, `library`, `language`,
  `task_categories`, `size_categories`. Fallback sources are paged completely, never truncated;
  a source that cannot answer is a `503` (retried by clients) unless `partial=ok`, which skips it
  and reports it in `X-Cheesecave-Incomplete`. The default sort is now `trending`, as on the Hub;
  `trending` ranks every readable repository; `updated` no longer merges external items by date
  (this hub's first, then each source).
- **Daily forward-looking huggingface_hub CI.** `hf-forward.yml` runs the tests marked
  `hf_client` against the latest release every day. A failure is triaged by
  `scripts/ci/hf_forward_triage.py`: an exact repeat comments on the open
  `hf-forward-ci` issue, a new problem opens one issue. A model decides near-duplicates
  through an Anthropic-compatible gateway configured in repository secrets.
- **Client compatibility matrix** in `docs/development/compatibility.md`.
- **`BRANDING.md`** describing names, attribution and fork naming.

- **Fallback cache: strict-freshness contract** ([#79](https://github.com/deepghs/KohakuHub/issues/79)).
  Cache key now includes `user_id` and a `tokens_hash` derived from the
  user's effective external tokens. Two users (or one user with
  different per-request `Authorization: Bearer ...|url,token|...` external
  tokens) can no longer share a binding. Three monotonic generation
  counters (`global_gen`, `user_gens[uid]`, `repo_gens[(rt, ns, name)]`)
  drive a `safe_set` race-protection check that rejects cache writes
  whose pre-probe snapshot disagrees with the post-probe state — closing
  the admin-mutation / token-rotation / repo-CRUD races where a probe
  in flight could otherwise pin a stale binding after an invalidation.
- **Fallback cache: invalidation hooks** ([#79](https://github.com/deepghs/KohakuHub/issues/79)).
  `cache.invalidate_repo(repo_type, ns, name)` and
  `cache.clear_user(user_id)` provide the two new eviction primitives;
  hooked into local repo create/delete/move/visibility-toggle, user
  external-token POST/DELETE/PUT bulk, and admin source create (the
  last one parallels the existing UPDATE/DELETE that already evicted).
- **Admin endpoints: per-repo and per-user fallback cache eviction**
  ([#79](https://github.com/deepghs/KohakuHub/issues/79)). New
  `DELETE /admin/api/fallback-sources/cache/repo/{repo_type}/{namespace}/{name}`
  and `DELETE /admin/api/fallback-sources/cache/user/{user_id}` for
  operational hygiene.
- **Fallback: repo-grain binding.** Within a single bind window every read
  against one `repo_id` goes to exactly one source. Eliminates cross-source
  mixing where the SPA showed source A's metadata while `resolve` served
  source B's bytes for the same repo. Implements the contract from #75.
  ([#77](https://github.com/deepghs/KohakuHub/pull/77))
- **Fallback: per-loop, per-repo binding lock.** Concurrent cache-miss
  callers serialize on the lock so all observers agree on the same bound
  source instead of independently scanning the chain. ([#77](https://github.com/deepghs/KohakuHub/pull/77))
- **Fallback: three-state classifier `FallbackDecision`.**
  `BIND_AND_RESPOND` / `BIND_AND_PROPAGATE` / `TRY_NEXT_SOURCE` in
  `kohakuhub.api.fallback.utils.classify_upstream`, mirroring
  `huggingface_hub.utils.hf_raise_for_status` priority order
  (`X-Error-Code` wins over numeric status). ([#77](https://github.com/deepghs/KohakuHub/pull/77))

### Changed

- **Environment names.** Settings are read from `CHEESE_CAVE_<NAME>` first; the existing
  `KOHAKU_HUB_<NAME>` names stay accepted as the fallback with the same suffix. Empty values still
  count as set. Compose variables keep their current names.
- **Licensing and contributions.** `LICENSING.md` now states the AGPL-3.0 licence and the origin of
  the fork; the upstream guide moved to `provenance/LICENSING.upstream.md`. `CONTRIBUTING.md` requires
  a Developer Certificate of Origin sign-off on every commit, enforced by the `DCO` workflow, and no
  longer grants commercial re-licensing rights.
- Tag `upstream-monorepo-last` marks the last upstream monorepo commit (`58f8101`) in this repository.

- **Fallback: `disabled` upstream marker now classifies as `TRY_NEXT_SOURCE`
  instead of `BIND_AND_PROPAGATE`.** When HuggingFace returns
  `X-Error-Message: "Access to this resource is disabled."` (moderation
  takedown), the chain now advances to the next source rather than
  immediately propagating `DisabledRepoError`. The aggregate response
  surfaces the `disabled` marker only when **every** source in the chain
  returns disabled. `huggingface_hub` clients are unaffected — they don't
  inspect chain internals — but tooling that depended on first-source
  `disabled` propagating verbatim must update. ([#77](https://github.com/deepghs/KohakuHub/pull/77))
- **Fallback: `with_repo_fallback` decorator gates the 404 fall-through on
  the local response's `X-Error-Code`.** A local 404 carrying
  `EntryNotFound` or `RevisionNotFound` is now authoritative ("the local
  repo exists; this entry/revision is missing") and is **not** dispatched
  to the fallback chain. The chain is entered only when the local layer
  signals `RepoNotFound` or returns a 404 without `X-Error-Code`.
  Restores the user-stated guarantee that a local repo wins absolutely on
  its namespace, regardless of upstream state. ([#77](https://github.com/deepghs/KohakuHub/pull/77))

### Fixed

- **Presigned S3 URLs are signed for the public address** ([#70](https://github.com/cheesecave/cheesecave-backend/issues/70)).
  Every presigned URL (download, upload, multipart part) was signed for
  `KOHAKU_HUB_S3_ENDPOINT` and then pointed at `KOHAKU_HUB_S3_PUBLIC_ENDPOINT`; SigV4
  signs the `Host`, so a store reached under two names refused them
  (`SignatureDoesNotMatch`). With the default MinIO signing it only showed for more than
  10 parts, because that path forced SigV4: the example compose could not upload an LFS
  file above about 500 MB. One client now makes all of them, signed for the public
  address; equal addresses (R2, AWS, production) are unchanged. A proxy in front of the
  store has to pass `Host` through (see `docs/deployment/production.md`), and the R2
  example there no longer uses an `r2.dev` public endpoint.

### Security

- **Authorization hardening on five routes.**
  - Organization members: only a super-admin may change or remove a super-admin,
    or grant that role; the last super-admin of an organization cannot be demoted
    or removed; the role must be one of `visitor`, `member`, `admin`, `super-admin`
    (`400` otherwise, also when adding a member). An organization admin can no
    longer change a super-admin's role through the UI or API.
  - `GET /api/quota/{namespace}` is limited to the user themselves or, for an
    organization, its members (`403` otherwise).
  - `DELETE /api/invitations/{token}` for a non-organization invitation requires
    the invitation's creator.
  - `DELETE /api/{type}s/{namespace}/{name}/like` answers a repository the caller
    cannot read exactly like a missing one.
  - `POST /api/validate-yaml` stays anonymous but refuses content above 1 MiB (`413`).

- **Git LFS routes: authorization is decided before anything is generated, and
  the follow-up calls carry a signed ticket.**
  - The batch route resolves the repository first. A repository that does not
    exist, or that the caller may not see, is answered like an unauthorized
    request: `401` with an `LFS-Authenticate` challenge for an anonymous
    caller, `404` for a signed-in one. An upload needs write permission, and
    the quota check runs before any upload URL is produced.
  - The `verify` and `complete` URLs the batch route hands out now carry a
    signed `ticket` (HMAC of purpose, repository, object id and, for
    `complete`, the multipart upload id; valid for as long as the part URLs).
    Both routes accept that ticket, because `huggingface_hub` sends no
    credentials on `complete` and the web UI sends none on either call, or a
    signed-in caller with write permission on the repository named in the URL.
    Anything else gets `401`. Completing a multipart upload through `verify`
    needs the signed-in writer.
  - Tickets are signed with `KOHAKU_HUB_SESSION_SECRET`. `compose.yml` already
    refuses to start without it; make sure it is not the default
    `change-me-in-production` (the config check warns about it), or tickets
    can be forged. Generate one with `scripts/generate_secret.py`.
  - Object ids must be lowercase SHA-256 hex on all three routes.
  - `POST .../git-receive-pack` answers `501` right after the permission checks,
    without reading the request body. It used to acknowledge a push while
    dropping the data, and parsing the pack as pkt-lines kept the worker busy.

### Removed

- **Board tooling and non-commercial documents.** The five KohakuBoard deployment and mock
  scripts, `examples/kohakuboard_cifar_training.py` and `docs/non-commercial/` are gone; KohakuBoard
  is maintained in its own repository. The `CONTRIBUTING.md` licence grant now names AGPL-3.0 only.

- **Dataset Viewer.** The `kohakuhub.datasetviewer` package, its seven
  `/dataset-viewer/` routes, the `disable_dataset_viewer` setting (the
  `KOHAKU_HUB_DISABLE_DATASET_VIEWER` environment variable is now ignored) and
  the dependencies only it used (`duckdb`, `fsspec`, `aiohttp`) are gone. The
  component was the only code in the repository under a separate,
  non-commercial license (Kohaku Software License 1.0), so the repository is
  AGPL-3.0 throughout, and removing it also reduces the attack surface. The
  dataset preview tab of the web UI is removed with it (cheesecave-web).

### Tracked

Planned follow-up work surfaced during the [#77](https://github.com/deepghs/KohakuHub/pull/77) risk review:

- [#78](https://github.com/deepghs/KohakuHub/issues/78) — lower default
  fallback cache TTL (`KOHAKU_HUB_FALLBACK_CACHE_TTL`) from `300` to `60`,
  decouple chain-probe logic into a pure `core.probe_chain` function, and
  add admin endpoints + frontend panel for real / simulated chain testing.
