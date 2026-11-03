# Collections

A collection is a curated list of repositories (and of other collections), as on Hugging Face. The routes, the field limits and
the answers follow Hugging Face's own OpenAPI spec and `huggingface_hub`, so `HfApi().create_collection(...)` and the web page
of a hub work against CheeseCave. Design notes and the checks behind them: [#134](https://github.com/cheesecave/cheesecave-backend/issues/134).

## What a collection is

- **Address:** `{owner}/{title-slug}-{24 hex}`, for example `alice/iccv-2026-6ae8b2220654892215f0a71c`. The id at the end never changes;
  the title slug follows the title. `{owner}/{title-slug}` (no id) also works and is the share URL. The same id behind another
  title answers `307` to the canonical address, so an old link survives a rename. The owner is a user or an organization.
- **Fields:** `title` (1 to 60 characters), `description` (at most 150), `theme` (one of `orange`, `blue`, `green`, `purple`, `pink`,
  `indigo`; drawn at random when the collection is made), `private`, `position` (the place in the owner's list), `upvotes`.
- **Items:** a model, a dataset, a Space or another collection (shown as a reference card, never expanded, so a loop of
  collections is harmless). Papers and buckets exist on Hugging Face and answer `501` here, and so do resource groups (an Enterprise feature: `resourceGroupId` on create and `POST .../resource-group` with a value answer `501`; `null` means none and is accepted). Each item can carry a plain-text
  note of at most 500 characters, answered as `{"text", "html"}` where the html has the text escaped and the web addresses linked.
  An item points to the repository row, so a renamed repository stays in its collections and a deleted one leaves them.
- **Positions** count up from 0 and may have gaps: deleting an item does not renumber the others; a new item goes after the
  highest.

## Who can see and change what

| | public collection | private collection |
|---|---|---|
| anyone | read | not found |
| the owner, or a member (`member`, `admin`, `super-admin`) of the owning organization | read and change | read and change |
| a `visitor` of the owning organization | read | read |
| anyone else | read | not found |

A collection one may not see is **`404`**, as for a repository (it does not reveal that it exists). Creating needs the right to
write in the namespace. **Items are filtered per viewer:** a repository the viewer cannot read is not in the answer, not in the
count of a collection that refers to it and not matched by the `item=` filter of a list. A user or an organization named
`collections` cannot exist (the name is reserved: it is a route).

## Routes

All under `/api`. Errors are `{"error": "message"}`.

| Method and path | What it does |
|---|---|
| `POST /collections` | `{title, namespace?, description?, private?, item?}` creates one (`item` puts the first item in at once). The same title slug under the same owner is `409` with `{"error", "slug"}` |
| `GET /collections/{owner}/{slug}` | one collection, with all its items; a weak `ETag` |
| `PATCH /collections/{owner}/{slug}` | `{title?, description?, theme?, private?, position?}`; answers `{"success": true, "data": collection}`. A title that makes another slug of the same owner is `409` |
| `DELETE /collections/{owner}/{slug}` | `204`; its items and upvotes go with it |
| `POST .../items` | `{item: {type, id}, note?}`; `type` is `model`, `dataset`, `space` or `collection` (`id` is the repository id or the slug); answers the collection. `404` for a repository that is missing or not readable, `409` for a duplicate, `400` for a collection in itself or over the limit |
| `PATCH .../items/{item id}` | `{note?, position?, gallery?}` (`gallery` is accepted and not stored yet); a position moves the item and renumbers the others |
| `POST .../items/batch` | `[{action: "update", _id, data}]`, all or nothing |
| `DELETE .../items/{item id}` | `204` |
| `POST /DELETE .../upvote` | give or take back the signed-in user's upvote; `{"success": true, "upvotes": n}` (the web page's; `huggingface_hub` has no method for it) |
| `GET /collections` | the list, below |

### The list

`GET /api/collections` takes `owner` (repeat it: any of them), `item=models|datasets|spaces|papers/<id>` (repeat it: a collection that holds any of
them, as on Hugging Face; `papers` matches nothing, a collection is not a valid filter), `q` (title and description, case-insensitive), `sort`, `limit` and `cursor`.

- `sort` is `trending` (the default: upvotes of the last 7 days, then the latest change), `upvotes`, `lastModified`, or CheeseCave's
  own `position` (the owner's order; Hugging Face answers `400` to it and a client never sends it).
- `limit` is 1 to 10000 (default 10), but **at most 100 unless `expand=false` and `owner` are given**: then each row is only
  `{name, private, title, upvotes}` (`name` is the full slug) and the items are left out.
- A list entry carries the first **four** items, and no `position` and no `shareUrl` (`huggingface_hub` passes `position=` itself).
- More rows: `Link: <absolute url with cursor=...>; rel="next"`. The cursor holds the key of the last row, so a page never
  repeats or skips a row when collections change between two requests. A cursor of another `sort`, or one made up, is `400`.

## Settings

`CHEESE_CAVE_COLLECTION_MAX_ITEMS` (default 1000) and `CHEESE_CAVE_COLLECTION_MAX_PER_OWNER` (default 500); `0` means no limit.
See [the configuration reference](../reference/config.md).

## Checking compatibility

The test suite checks it two ways. `test/kohakuhub/api/test_huggingface_hub_collections.py` drives every client method above with
the real `huggingface_hub` (it runs in every client version of the CI matrix, 0.20.3 being the oldest with collections), and
`test/kohakuhub/api/test_collections_hf_spec.py` validates the answers against the JSON Schemas of Hugging Face's OpenAPI spec
(`https://huggingface.co/.well-known/openapi.json`; the schemas used are kept in `test/kohakuhub/support/hf_collections_openapi.json`).
