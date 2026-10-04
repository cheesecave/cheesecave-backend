# Following and workspace activity

Local users can follow people and organizations. Following expresses interest; it never grants
membership, repository access, or permission to act as an organization. Organizations cannot follow
accounts. Self-following and following inactive accounts are rejected. Remote fallback profiles do
not create local relationships.

## API

All routes below use the configured API prefix, normally `/api`, and return `Cache-Control: no-store`.

| Route | Authentication | Result |
| --- | --- | --- |
| `GET /users/{username}/follow` | Optional | Follow state and counts |
| `PUT /users/{username}/follow` | Active individual | Idempotent follow |
| `DELETE /users/{username}/follow` | Active individual | Idempotent unfollow |
| `GET /users/{username}/followers` | Optional | Cursor-paginated followers |
| `GET /users/{username}/following` | Optional | Cursor-paginated followed accounts |
| `GET /workspace/feed` | Active individual | Cursor-paginated activity |

Follow state contains `username`, `is_org`, `following`, `can_follow`, `followers_count`, and
`following_count`. Anonymous viewers receive `following: false` and `can_follow: false`. Follow lists
contain `items`, `has_more`, and `next_cursor`. Each item contains `username`, `full_name`, `is_org`,
`avatar_url` (nullable), and `followed_at`. Emails, membership lists, and avatar blobs are not exposed.
Lists accept `limit` (default 20, range 1–100) and an opaque `cursor`.

The feed accepts `scope=all|self|organization|following|personal` (default `all`),
`repo_type=all|model|dataset|space` (default `all`), `event_type=all|repository|like` (default `all`),
`limit` (default 20, range 1–100), and `cursor`.

Event category and repository type are independent filters. `repository` includes only repository
creation and main-branch commit events; `like` includes only actual likes. The UI's Models, Datasets,
and Spaces categories pass their repository type together with `event_type=repository`. Its Likes
category passes `repo_type=all&event_type=like`; All omits `event_type`. An older request specifying
only `repo_type` still receives all event kinds for that type. Category filtering excludes unused
SQL sources before cursor boundaries and LIMIT, so likes cannot consume a repository-category page.

- `personal`: events performed by the viewer, or occurring in the viewer's own namespace or an
  organization where the viewer currently has membership, including the visitor role.
- `following`: repository creation events in followed namespaces; commits and likes performed by
  followed individual accounts. Following a person does not include other people's actions on that
  person's repositories. Followed organizations aggregate events in repositories they own.
- `all`: the union of Self, Following, and events in repositories owned by the viewer's current
  member organizations, with each event appearing once. Other people's actions on the viewer's
  personally owned repositories are included only if those actors qualify through Following.
- `self`: repository creation events whose current owner is the viewer; commit and like events
  whose actual actor is the viewer. Repository ownership never attributes somebody else's commit
  or like to the viewer. Repository read permission still applies to every event.
- `organization`: creation, commit, and like events in repositories owned by the organization named
  in `organization=<username>`. Current membership is required for every request, including public
  repositories and each cursor page; all existing member roles qualify. The scope does not include
  members' activity in other namespaces. Missing/empty organization, or supplying organization with
  another scope, returns 400. A nonmember, former member, missing organization, or non-organization
  account returns the same 403 response. Following never replaces membership. Cursors bind to the
  selected organization's local identity; using one in another organization returns 422.

The legacy `personal` scope remains available for existing clients with its original meaning,
including other actors' events on personally owned repositories. The main UI uses `self`, labeled
**Self**, while `all` continues to aggregate the viewer's member organizations. Creation rows do not
record the original creator: their `actor` remains null and their current owner namespace identifies
the repository, rather than claiming that the owner created it.

Every SQL source filters the repository's current visibility, interest scope, type, and pagination
boundary **before** limiting rows. Following an organization therefore exposes its public activity;
private activity additionally requires current membership. Following one contributor does not
implicitly follow all colleagues in that contributor's organizations. Read permissions are
reevaluated on every page. Each page uses a consistent database read snapshot, preventing concurrent
cascades from producing a partial actor/repository projection.

The shared read policy uses the repository's `owner` foreign key as its authority. A namespace
string is an additional author filter, never an access grant. Ordinary repository lists, user
overviews, discovery facets/counts, individual repository reads, and feed cursor boundaries use
this same policy. Organization membership remains a live SQL subquery, including all existing
member roles; following is separate. Legacy public trending lists retain their public-only scope,
with visibility and author filtering applied before the top-result limit.

Feed responses contain `items`, `has_more`, and `next_cursor`. Events contain:

```json
{
  "id": "commit:123",
  "kind": "commit",
  "created_at": "2025-01-01T12:00:00Z",
  "actor": {
    "username": "alice", "full_name": "Alice", "is_org": false, "avatar_url": null
  },
  "namespace": { "username": "team", "is_org": true },
  "repository": {
    "id": "team/model", "type": "model", "private": false,
    "lastModified": "2025-01-02T12:00:00Z", "likes": 12, "downloads": 340
  },
  "commit": { "sha": "stored-main-sha", "message": "Actual commit message" }
}
```

Kinds are `repo_created`, `commit`, and `like`; their IDs combine the kind with the corresponding
source row ID. Creation events always have `actor: null`: repositories do not record their original
creator. Commit actors come from `Commit.author`, not the owner or denormalized username. Like actors
and times come from the current `RepositoryLike` row. Commit events include only stored `main`
branch commits; messages are plain text capped at 1,000 characters. Other events have `commit: null`.
The namespace reflects the repository's current owner.

Repository cards show current `likes` and `downloads` counters, independent of the event's
historical `created_at`. Their `lastModified` is the latest stored `main` commit time, resolved
in one batch with the same helper used by repository listings. When no stored main commit exists,
the feed uses repository creation time; it does not make a LakeFS request per activity card.
These fields are read on every page within its database snapshot and need no schema migration.

All response times use explicit UTC `Z`. Legacy timezone-naive timestamps follow the application's
UTC convention; SQLite values with explicit offsets are converted to UTC. Stable descending cursor
ordering uses UTC milliseconds, event-kind rank (`like`, `commit`, `repo_created`), then source row ID.
Cursor state fixes each source's maximum row ID for that paging session, so later insertions do not
shift older pages. Cursors bind to viewer, list/feed identity, scope, repository type, event category,
and the actor-attribution semantics. Organization cursors also bind to the organization identity.
Changes require starting from the first page; cursors from the earlier attribution rules are rejected.
Invalid cursors return 422. `has_more` comes from one additional
visible row, without disclosing a count of inaccessible activity. Each source supplies at most
`limit + 1` rows to the final SQL union; no complete-catalog Python filtering is used.

## Persistence and history limits

Migration `030_user_follow.py` adds only `user_follow`: an auto ID, follower/followed foreign keys
to the unified local user table, and a UTC creation timestamp. Both foreign keys cascade on account
deletion. A unique follower/followed pair makes repeated or simultaneous first writes idempotent,
and a check prohibits self-following. Composite indexes support follower and followed lists. Fresh
database initialization includes the model. The migration supports SQLite and PostgreSQL, verifies
foreign-key targets and cascade behavior, preserves existing settings/repository rows, and can be
rerun. An incompatible existing table fails validation without destructive repair. Use the normal
`python scripts/run_migrations.py` upgrade procedure before starting the updated API; back up the
database first as described in the deployment guide.

Activity is a derived view of existing repository creation, commit, and active like rows, rather
than an immutable event log. Existing stored activity is available immediately; no history backfill
or separate worker is required. Unliking removes its event, repository deletion removes its events,
and squashing/deleting old stored commits removes those commit events. Deleting a commit's author
can cascade its recorded commits. Transfers move existing events to the current namespace and can
change which interest scope includes them. Unrecorded external LakeFS changes, imports without
stored commit rows, and the placeholder Git-push hook produce no invented activity. A future durable
audit history would require a separate event store and explicit write-path integration.
