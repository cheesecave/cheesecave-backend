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

Repository Settings contains request review, code issuance/revocation, account grant
management, guest-session revocation and an audit log. Management lists are paginated.
Read-only grantees do not acquire write or management privileges.

## Repository codes and registration

Both Private and Invite repositories support repository invitation codes. New codes
have 256 bits of random material and are stored as SHA256 hashes. A secret is shown
once when issued and is absent from subsequent list and audit responses.

A code can be bound to an existing account, supplied during registration, or exchanged
for a temporary browser session. The `/repo-access` page provides explicit account
binding and guest exchange, including for a Private repository whose ordinary URL
does not expose an application page.

Registration accepts `repo_invitation_token` in the JSON body. Older links may pass a
`repo_` code through the `invitation_token` query parameter in open registration mode.
A code is processed even when the site does not require an invitation. User creation,
account redemption count, redemption history and repository grant commit together;
an invalid, exhausted, revoked or expired code does not leave a new account behind.

Repository codes do **not** satisfy the site's `invitation_only` policy. On such a site,
registration also requires a valid site `register_account` invitation through the
existing `invitation_token` parameter. The site invitation and repository code are
consumed in the same transaction. Organization membership, if explicitly configured
by that separate site invitation, retains its existing site-invitation meaning; the
repository code itself never creates membership.

## Independent lifecycles

| Item | Default / limit | What expiry or revocation changes |
| --- | --- | --- |
| Code exchange deadline | 7 days; HTTP maximum 90 days | Expiry stops new exchanges |
| Account exchanges per code | 1; configurable up to 10,000 | One consumption per distinct account |
| Guest sessions issued per code | 1; configurable 0–10,000 | Separate total-issuance quota, not the account quota |
| Guest session lifetime | 24 hours; configurable 60 seconds–7 days | Stops that session's previews |
| Account grant lifetime | No expiry unless configured | Independent from code/session deadlines |

Revoking a code stops new exchanges and revokes its derived guest sessions. Already
bound account grants remain until a manager separately revokes or changes them.
Re-exchanging the same code/account pair is idempotent and never restores a revoked
or expired grant. A manager can explicitly renew the account grant. Code exchange
expiry does not shorten an already issued guest session.

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

First-release preview boundaries:

- Regular UTF-8 text/code/Markdown/JSON: plain text, at most 8 KiB and 64 lines.
- Regular CSV/TSV/JSONL: first 20 data rows, 20 columns and 256 characters per cell,
  from a bounded 64 KiB prefix.
- Regular PNG/JPEG/WebP/GIF: a reencoded thumbnail of at most 512 pixels per side,
  using only the first frame and removing source metadata. Source limit: 2 MiB and
  16 million pixels. SVG is unsupported.
- LFS-backed files, PDF, audio, video, Parquet, TAR/ZIP and other archives, model
  weights and browser model inference: metadata only; no original file preview.
- The tree shows at most 100 entries per folder and reports truncation. Previews
  use the current default branch only. Arbitrary refs, byte ranges, offsets and
  preview pagination are not available.

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
| GET `/manage?limit=100&offset=0` | Manager lists and per-section `has_more` flags |
| POST `/requests/{id}/review` | Manager approve/reject |
| POST `/invites` | Manager issue; one-time `token` response |
| POST `/invites/{id}/revoke` | Revoke code and derived guest sessions |
| POST `/grants` | Manager grant by account username |
| POST `/grants/{id}/revoke` | Revoke an account grant independently |
| POST `/bind` | Authenticated account exchange |
| POST, DELETE `/guest` | Establish/end the caller's guest session |
| POST `/guests/{id}/revoke` | Manager revoke one guest session |
| GET `/guest/tree`, `/guest/preview` | Dedicated bounded previews |

For nonlocal repositories and unauthorized ordinary-private repositories, preflight
returns the same opaque `standard` response. The normal repository APIs then enforce
their existing masked denial or external fallback policy. Invitation repositories
are intentionally discoverable by link, but remain absent from anonymous discovery.

## Upgrade and validation

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
