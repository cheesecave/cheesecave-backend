# Repository discovery

The Models, Datasets and Spaces discovery pages filter the complete visible
local catalog using README frontmatter. Filtering and totals apply before
pagination, so repositories beyond the first hundred results remain searchable.
Private repositories, their facets and indexing counts are available only to
their namespace owner and organization members. External fallback repositories
remain on the existing Hugging Face list APIs; discovery currently indexes local
repositories only.

## API

`GET /api/models/discover`, `/api/datasets/discover` and `/api/spaces/discover`
accept `search`, `sort` (`trending`, `recent`, `updated`, `likes`, `downloads`),
`limit` (default 24, maximum 100) and `offset`. Trending uses the existing activity
score, applying visibility and facets before pagination. Ties use repository ID
for stable pages. Updated dates come from batched main-branch commit records.

Repeat `task`, `library`, `language`, `license`, `tag`, `size`, `format`, `modality`
or `sdk` parameters to select several values. Values in the same dimension use
OR; dimensions combine with AND. Unknown selected values return zero matches
and remain in the facet options with a zero count. Facet option counts use the
complete visible catalog and the other selected dimensions, excluding their
own dimension's selection.

The response contains `items`, `total`, `has_more`, `selected`,
`facets: [{key,label,options:[{value,count}]}]`, and
`indexing: {pending,total}`. Item `metadata` contains normalized declared README
fields as string arrays; item `facets` maps dimensions to their lowercase values.
`tags` includes explicit README tags. Search matches repository identifiers.
`selected` contains canonical, deduplicated filter arrays using Python Unicode
case folding; clients use them to keep their selected options and URL synchronized.
Responses send `Cache-Control: no-store`. Existing Hugging Face list endpoints
retain their shapes and behavior.

## Indexing and freshness

Migration `029_repository_discovery` creates `repository_metadata` and
`repository_facet`. Fresh installations create them automatically. These tables
reference repository rows and cascade on deletion; existing branding, homepage,
appearance and repository data are preserved.
The migration rejects incompatible existing foreign-key targets or delete
actions rather than changing those tables or their saved rows. Run upgrades normally:

```sh
python scripts/run_migrations.py
```

Discovery starts a bounded background batch in the API process, so initial
responses do not wait for storage reads and do not require a separate worker.
Each batch processes at most 40 repositories with four concurrent reads and a
25-second deadline. Database leases prevent multiple API processes from
publishing duplicate or superseded work. The client can poll while `pending`
is positive. Facets and filtered totals may be incomplete during initial
indexing; the progress indicator reports this explicitly. Unfiltered browsing
includes repositories not yet indexed.

Main-branch commits through the upload and history APIs invalidate metadata
immediately. Repository moves also invalidate it. Missing or invalid README
frontmatter completes as an empty index instead of blocking discovery. Stored
snapshots are refreshed after five minutes to catch out-of-band LakeFS changes.
The TTL refresh preserves usable cached facets until a known main change
invalidates them. Transient failures retry after 30 seconds and leave progress
pending; failed refreshes preserve an existing ready snapshot when no main
change has been observed. The current Git receive-pack implementation is a
placeholder and does not provide a working metadata push hook.

The index reads an immutable main commit and tries `README.md`, `readme.md`, then
`Readme.md`, advancing only after a 404. The first existing file supplies metadata;
an invalid card does not cause another name to be tried. Each fetch reads at most
the first 64 KiB, and all candidate attempts share one three-second deadline.
The index checks main again before publishing; branch reads also time out after
three seconds. Streams remain bounded even if storage ignores Range.
Frontmatter must start with `---` and close within that prefix; plain, missing,
malformed or oversized frontmatter supplies no facets. YAML aliases, excessive
nesting and more than 2048 nodes are rejected. Each recognized field retains at
most 100 short string values. Metadata never instructs the server to fetch URLs.

| Facet | Declared README fields |
| --- | --- |
| Task | `pipeline_tag`, `task_categories` |
| Library | `library_name` |
| Language | `language` |
| License | `license` |
| Tag | `tags` |
| Dataset size | `size_categories` |
| Format | `format`, `formats`, or `format:value` tags |
| Modality | `modality`, `modalities`, or `modality:value` tags |
| SDK | `sdk` |

Formats and modalities are derived from declared metadata; the index does not
guess them from files that it has not scanned.
