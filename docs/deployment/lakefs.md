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
| below 1.48.0 | **Unsupported** | Reset reports success but leaves a merge commit with two parents instead of one linear commit. KohakuHub disables Reset on these releases. |
| 1.48.0 | **Unsupported** | LakeFS's own "do not use" release: it squashes every merge by default. |
| 1.48.1 – 1.69.x | Supported | |
| 1.70.0 | **Unsupported** | Cannot store regular files on an S3 endpoint without TLS, such as the bundled MinIO. Fixed in 1.70.1. |
| 1.70.1 – **1.86.0** | Supported | **1.86.0 is the bundled release**, and the last one under Apache 2.0. |
| 1.87.0 | Supported, **BSL 1.1** | Works, but under a different license: see [License](#license). |
| newer | Untested | |

How this was determined:

- The full backend suite (1,444 tests) passes on 1.48.1, 1.70.1, 1.86.0 and
  1.87.0.
- Its LakeFS-heavy part (186 tests: Super Squash, Reset, Revert, commit
  availability, garbage collection, storage usage, Hugging Face compatibility)
  passes on every sampled release from 1.48.1 to 1.87.0 except 1.70.0.
- Releases below 1.48.1 fail the Reset tests.

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
