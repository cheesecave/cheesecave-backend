---
title: LakeFS Compatibility
description: Which LakeFS releases KohakuHub supports, why, and LakeFS's license change.
icon: i-carbon-information
---

# LakeFS Compatibility

KohakuHub keeps every repository's files and history in LakeFS. Not every LakeFS
release works with it, and LakeFS changed its license in 1.87.0.

## Supported releases

| LakeFS | Status | Why |
| --- | --- | --- |
| below 1.48.0 | **Unsupported** | Reset is not verified on these releases, so KohakuHub disables it. The earlier, merge-based Reset left a merge commit with two parents here; the current one commits a metarange, which LakeFS has offered since 1.0, but has not been run here. |
| 1.48.0 | **Unsupported** | LakeFS's own "do not use" release: it squashes every merge by default. |
| 1.48.1 – 1.69.x | Supported | |
| 1.70.0 | **Unsupported** | Cannot store regular files on an S3 endpoint without TLS, such as the bundled MinIO. Fixed in 1.70.1. |
| 1.70.1 – **1.86.0** | Supported | **1.86.0 is the bundled release**, and the last one under Apache 2.0. |
| 1.87.0 – 1.88.0 | Supported, **BSL 1.1** | Works, but under a different license: see [License](#license). |
| newer | Untested | |

How this was determined:

- The full backend suite (about 1,470 tests) passes on 1.48.1, 1.70.1,
  1.86.0, 1.87.0 and 1.88.0.
- Its LakeFS-heavy part (186 tests: Super Squash, Reset, Revert, commit
  availability, garbage collection, storage usage, Hugging Face compatibility)
  passes on every sampled release from 1.48.1 to 1.88.0 except 1.70.0.
- Releases below 1.48.1 failed the Reset tests of the earlier, merge-based
  Reset. The current Reset has not been run on them.
- These runs used the earlier Reset. The current one, a metarange commit, is
  run by CI on 1.48.1 and 1.86.0.

CI runs the suite on 1.86.0 and on 1.48.1, the oldest supported release.

## The bundled release

These pin `treeverse/lakefs:1.86.0`, never `latest`:

- `docker/lakefs/Dockerfile`, which the Docker Compose bundle builds;
- the development stack (`scripts/dev/up_infra.sh`);
- CI.

Upgrading LakeFS is a deliberate change:

1. Run the backend suite against the new release.
2. Add the release to `kohakuhub/lakefs_compat.py` and to this page.
3. Then bump the pins.

Releases newer than the newest tested one are reported as untested, not
refused.

## An S3 endpoint with the bucket in its path

With `LAKEFS_BLOCKSTORE_S3_ENDPOINT` ending in a bucket (`https://<account>.r2.cloudflarestorage.com/<bucket>`, see [production](production.md#external-s3)), LakeFS's storage namespaces name a key prefix inside that bucket. Object requests work, which is all KohakuHub needs: uploads, reads, commits, merges, reverts, links and repository creation (#133). Requests that name the bucket anywhere else do not:

- `objects/copy`, a copy through the S3 gateway, and copying a multipart part: 403. KohakuHub's Reset copies nothing.
- Import (`ImportStart`) and the S3 gateway's multipart-upload listing: they list the bucket, which goes wrong.
- LakeFS's own garbage collection job (Spark): it lists and bulk-deletes. KohakuHub collects garbage itself.
- Bucket region discovery: one anonymous probe of the prefix, which fails. LakeFS falls back to the configured region and logs an error. Set `LAKEFS_BLOCKSTORE_S3_DISCOVER_BUCKET_REGION=false` to skip it.
- Keep `installation.allow_inter_region_storage` at its default (`true`). With `false`, LakeFS checks each new repository's region with that same probe, and repository creation fails.

The CI job "backend tests (S3 endpoint with a path)" runs the backend suite this way, so a change that brings one of these calls back fails there.

## License

LakeFS 1.87.0 changed its license from Apache 2.0 to the
[Business Source License 1.1](https://github.com/treeverse/lakeFS/blob/master/LICENSE)
([release notes](https://github.com/treeverse/lakeFS/releases/tag/v1.87.0)).

- **Production use:** its Additional Use Grant allows it only for the unmodified
  release and for your organization's internal use.
- **Offering it to others:** you may not host or offer the software or its
  functionality to third parties, free or paid.
- **Back to Apache 2.0:** each release becomes Apache 2.0 four years after its
  publication.

A public hub serves its versioned repositories to outside users. Whether that
fits these terms is for each deployment to assess; this page is not legal
advice. Releases up to 1.86.0 remain under Apache 2.0, which is why KohakuHub
bundles 1.86.0.

## Checking a deployment

- **Admin portal → Health:** the LakeFS card shows the version, whether it is
  supported, and a "BSL 1.1" tag for 1.87.0 and later.
- **Startup log:** the API service reads the LakeFS version when it starts:
  - an unsupported release is logged as an error;
  - an untested or BSL-licensed release, as a warning.

  If LakeFS is not up yet, the version is read again later.
- **Reset on an unsupported LakeFS:**
  - `GET /api/site-config` reports `reset: false`;
  - the button is hidden;
  - the endpoint answers `503 operation_disabled` and says why.
