# CheeseCave Backend: original source and Git history

Derived from [KohakuHub by KohakuBlueLeaf](https://github.com/KohakuBlueleaf/KohakuHub)
and [the deepghs fork](https://github.com/deepghs/KohakuHub).
This is an independent CheeseCave fork, not an official release of either upstream project.

## Source snapshot

- Original repository: https://github.com/deepghs/KohakuHub.git
- Original branch: `main`
- Original commit: `58f81016722c2f1957b570deefd9b4c0eef68f9a`
- Planned destination: https://github.com/cheesecave/cheesecave-backend
- CI removal preparation commit (in backend history): `0b4051747ffddc7d2dae436f897c7fcdb996fe15`
- Split date: 2026-10-04 (Asia/Hong_Kong).

## Complete original Git history retained

The backend directly inherits all 760 original remote commits. Their SHA values,
parent links, authors, committers, dates, messages and any signed commit objects
remain unchanged. Historical trees contain original monorepo components. A new preparation commit
removes inherited GitHub Actions and Codecov configuration before splitting.
The following CheeseCave commit removes frontend components from the current tree and adds
standalone backend build/deployment configuration. No original commits are
path-filtered or rewritten here.

The web/admin repositories instead begin fresh with **从原仓库分叉**, containing
their extracted source snapshot after CI removal, without original upstream Git commits. Later
commits adapt the frontend builds and apply genuine unpublished local work.

Local work already merged remotely is not replayed. The original working repo and
a verified full-history Git bundle remain backed up outside these repos. The
original working repository has not been archived.

## Licenses, attribution and compatibility

Original root LICENSE and LICENSING.md texts are unchanged. Core code retains
AGPL-3.0. The backend Dataset Viewer, which retained its separate original
license at `src/kohakuhub/datasetviewer/LICENSE`, was removed on 2026-10-08; its
license text and history remain in the git history. Rebranding and splitting do
not replace those terms or original copyright/author notices. See `README.upstream.md` and
`CHANGELOG.upstream.md` for original project information.

Python imports, KOHAKU_HUB variables, API/protocol identities, database migrations
and storage mappings remain compatible. CheeseCave is the new default display
and distribution name. No production data was migrated and no inherited issue
was closed or claimed resolved by this split.
