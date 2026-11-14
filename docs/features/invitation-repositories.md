# Invitation repositories

An invitation repository is a private repository with a public access-application page.
It is available by its link; enabling invitation mode does not add it to public search.
Normal public and private repositories retain their existing behavior.

## Visibility and Hugging Face compatibility

The stored `private` boolean remains the Hugging Face access boundary. `invite_mode` is
an additional CheeseCave setting, with a database constraint requiring `private=true`.

| Product state | `private` | `invite_mode` |
| --- | --- | --- |
| Public | false | false |
| Private | true | false |
| Invite | true | true |

Creation and settings accept the existing HF `private` and `visibility` request shapes.
`visibility` retains the HF `public`/`private` vocabulary; use `invite_mode` for the
invitation setting. Enabling invitation mode implies private unless the same request
explicitly requests public, which is rejected without applying any changes.
An HF update to private preserves an existing invitation setting. An explicit update
to public clears it. `invite_mode=false` alone changes Invite to Private, not Public.

This feature does not implement HF gated repositories or gated-access APIs. Existing
`gated` responses and refusals remain unchanged. Registered readers use their own
normal personal access-token for HF clients. A repository invitation code or guest
cookie is never an HF credential.

## Public instructions and applications

The browser checks repository access before mounting the normal repository viewer,
including direct tree, blob and history links. Unapproved visitors see only the
default branch's root `INVITE.MD` and application/code controls. Missing, oversized,
non-UTF-8 or LFS-pointer instructions use the default application page. The public
instruction size limit is 64 KiB. Storage errors fail closed with a retryable error.

The public Markdown renderer does not load embedded media or relative/private file
resources. Publishing `INVITE.MD` does not publish README, sibling files, history,
original file addresses or the resources mentioned by the instructions.

Signed-in applicants are reviewed by the repository owner or its organization's
admin/super-admin. Approval grants that account read/download access to this
repository directly. It does not create a code or an organization membership.
Anonymous applications require an email address and receive an opaque status
receipt, stored only as a hash on the server. Anonymous approval changes the request
status; a manager may then explicitly issue an associated code and share it with the
applicant. There is no automatic email delivery or automatic approval.

Repository Settings has four independently paginated tables for applications, repository
codes, account grants and the audit log. Managers can review applications, issue and
revoke codes, and revoke account grants. Individual guest-session revocation remains
available through the management API.
Read-only grantees do not acquire write or management privileges.

## Repository codes and registration

Both Private and Invite repositories support repository invitation codes. New codes
have 256 bits of random material and are stored as SHA256 hashes. A secret is shown
once when issued and is absent from subsequent list and audit responses.

At `/repo-access`, sign in, enter a repository code and select **Open repository**.
The server resolves the code to its repository, grants the current account read
access and returns the repository URL. This works with codes for Private and Invite
repositories. The invitation page of an Invite repository offers temporary preview.

The registration form accepts a site or repository code in one **Invitation code**
field. The server classifies the code. **Sign Up** in the invitation dialog or an
eligible visitor preview carries the repository invitation into registration. After
registration and any required email verification, sign-in returns to the repository.
Invitation-only sites also require a site `register_account` invitation.

User creation, site invitation consumption, repository code consumption and the
repository grant commit together. Organization membership can come from a site
invitation that explicitly grants it. For request fields and compatibility, see
[Authentication API](../api/authentication.md).

## Independent lifecycles

| Item | Default / limit | What expiry or revocation changes |
| --- | --- | --- |
| Code exchange deadline | 7 days; HTTP maximum 90 days | Expiry stops new exchanges |
| Account exchanges per code | 1 | Binding or registration closes the code to further new exchanges |
| Guest sessions issued per code | Repeatable while the code is valid and unbound | Guest access does not consume the account exchange |
| Guest session lifetime | 24 hours; configurable 60 seconds–7 days | Stops that session's previews |
| Account grant lifetime | No expiry unless configured | Independent from code/session deadlines |

Revoking a code stops new exchanges and revokes its derived guest sessions. Already
bound account grants remain until a manager separately revokes or changes them.
Re-exchanging the same code/account pair is idempotent and never restores a revoked
or expired grant. A manager can explicitly renew the account grant. Code exchange
expiry or account binding does not shorten an already issued guest session.
Revoking an account grant blocks subsequent authorized requests. Download and Xet
URLs that were already signed retain their own expiration.

## Temporary browser previews

Guest sessions are separate repository-scoped identities carried by independent,
expiring, HttpOnly cookies. They do not overwrite login cookies or personal tokens.
If a logged-in account is independently authorized, the normal account view wins;
otherwise a valid guest session permits only the restricted browser viewer.

The guest identity is recognized exclusively by dedicated controlled-preview APIs.
It does not authorize normal repository APIs, raw GET/HEAD/Range downloads, Git, LFS,
Xet/CAS, dataset raw data, fallback downloads, writes, account management or token
creation. Revocation and scope are checked on each preview request. No original or
presigned address is sent to the guest.

The Web reuses the ordinary repository blob workbench, file tree, commit header,
Markdown parser/themes, PDF reader and native media player. A controlled data
source changes the permitted requests and actions; there is no separate guest
file-preview page or Markdown renderer. Raw, original URL, download, edit and delete
controls are absent for guests, and backend authorization independently refuses
original-file and bulk-download routes.

Controlled preview boundaries:

- Canonical root README Card: at most 64 KiB / 1000 lines. Other regular UTF-8
  text/code/Markdown/JSON: at most 8 KiB / 64 lines, presented by the normal components.
- CSV/TSV/JSONL: first 20 data rows, 20 columns and 256 characters per cell, from a 64 KiB
  prefix. PNG/JPEG/WebP/GIF: reencoded metadata-free, first-frame thumbnails up to
  512 pixels per side; source limit 2 MiB / 16 million pixels. SVG remains unsupported.
- PDF: the same page/zoom/canvas reader used by ordinary repositories; guests
  receive only rendered PNG pages, never source PDF bytes. Source limit 64 MiB,
  1000 pages, page rasters up to 1600 pixels on the long edge. PDFium native parsing
  runs in a separate 20-second child process; PDF scripts/forms are not executed.
- Video/audio: the same player used by ordinary repositories, with playback and
  seeking. Guest sources are re-encoded H264/AAC HLS fragments, not copied original
  streams or byte-range proxies. Source limits 256 MiB video / 128 MiB audio, duration
  up to 10 minutes, video up to 1280x720. Files beyond a processing limit return an
  explicit preview error; supported media no longer falls back to metadata-only.
- PDF/audio/video stored via LFS can be previewed only with a matching nondeleted,
  repository-scoped File record and verified physical-object SHA256. Other LFS
  content, Parquet, TAR/ZIP and other archives, weights and browser inference
  remain metadata-only. No global object lookup grants guest access.
- All previews use the current main snapshot. Tree 100-entry pages and 20-commit
  history pages use signed, scope-bound cursors; complete listing is available by
  pagination. Filename prefix filtering is local to the selected directory.
  Callers cannot choose arbitrary file revisions, source ranges or offsets.
- Media descriptors bind an immutable repository/head/path snapshot. Every page,
  playlist and segment rechecks guest authorization and the current head, including
  cache hits and completed processing. Revocation denies new fragments; a changed
  head returns 409 and requires reopening. No raw or presigned address is returned.
- A private disposable cache holds at most 4 documents/media entries with 15-minute
  idle lifetime, at most 2 native jobs, and 256 MiB generated output per job. Video
  processing has a 90-second deadline; source reads 60 seconds. Network and playlist/
  concat input demuxers are disallowed. Original media sources are removed after
  transcoding; PDF sources remain private only for on-demand page rendering.

PDF rendering uses the installed pypdfium2 package; imageio-ffmpeg supplies its
platform FFmpeg binary, so the backend does not depend on a desktop application's
PATH. Deploy with the declared Python dependencies and retain dependency licenses,
including the actual bundled FFmpeg build's license. The Web declares PDF.js and
HLS.js dependencies. Native-decoder crashes/timeouts are reported as preview errors,
not permission misses and not triggers for upstream fallback.

The product promise is **no original-file or bulk download for guests**. Material
already displayed can still be copied or saved; this is not absolute copy protection.

## HTTP surface

All paths below are relative to `/api/{type}s/{namespace}/{name}/access`, where type
is `model`, `dataset` or `space`. These are CheeseCave browser APIs, not HF gated APIs.

| Method / suffix | Purpose |
| --- | --- |
| GET (root) | Preflight: `full`, opaque `standard`, `invite` or `guest` |
| POST `/requests` | Submit an account or email application |
| GET `/request` | Current account's status, or anonymous status authenticated by `X-Repository-Request-Receipt` |
| GET `/manage?section=requests&limit=20&offset=0` | One paginated manager section with counts and `has_more` |
| POST `/requests/{id}/review` | Manager approve/reject |
| POST `/invites` | Manager issue; one-time `token` response |
| POST `/invites/{id}/revoke` | Revoke code and derived guest sessions |
| POST `/grants` | Manager grant by account username |
| POST `/grants/{id}/revoke` | Revoke an account grant independently |
| POST `/bind` | Authenticated account exchange |
| POST, DELETE `/guest` | Establish/end the caller's guest session |
| POST `/guests/{id}/revoke` | Manager revoke one guest session |
| GET `/guest/overview`, `/guest/file-info` | Scoped repository/card and file metadata |
| GET `/guest/tree`, `/guest/commits` | Scoped complete paginated listing/history |
| GET `/guest/preview` | Controlled text/thumbnail/rows or PDF/media descriptor |
| GET `/guest/media/pdf/page?path=...&page=1&snapshot=...` | Authorized rendered PNG page |
| GET `/guest/media/hls/playlist?path=...&snapshot=...` | Authorized derivative HLS manifest |
| GET `/guest/media/hls/segment?path=...&snapshot=...&segment=0` | Authorized re-encoded fragment |

The account-only `POST /api/repo-access/redeem` takes `{"token":"<repository-code>"}`
without a repository identifier. It requires normal authentication and returns
`success`, `grant` and `repository_url`. A repeated redemption can return an inactive
grant, so clients should check the grant before opening the repository.

For nonlocal repositories and unauthorized ordinary-private repositories, preflight
returns the same opaque `standard` response. The normal repository APIs then enforce
their existing masked denial or external fallback policy. Invitation repositories
are intentionally discoverable by link, but remain absent from anonymous discovery.

## Upgrade and validation

The [release guide](../deployment/invitation-repositories.md) records review findings,
validation evidence, migration checks, Backend/Web/Admin cutover order and rollback.

Apply migrations 043 and 044 with the normal migration runner before starting this
release against an existing database. Fresh databases use `init_db`. Coordinate the
backend and Web release: the new Web performs access preflight before repository
loads. Admin's three-state display requires the backend's `invite_mode` field and
remains compatible with older private-only responses.

See [the implementation record](../plans/invite-repo.md) for exact branches, commits,
checks, known environment/baseline failures and remaining integration work. This
document describes the implemented feature boundary; the record is authoritative
about which release checks have actually completed. No production rollout is part
of the implementation task.
