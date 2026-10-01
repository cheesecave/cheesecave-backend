"""Which LakeFS versions KohakuHub works with (docs/deployment/lakefs.md).

Each boundary was found by running the backend suite against the LakeFS
release in question:

- below 1.48.1, Reset reports success but leaves a merge commit with two
  parents instead of one linear commit: it relies on squash merges, which
  LakeFS added in 1.48.0 (and 1.48.0 itself squashes every merge by default,
  LakeFS's own "do not use" release);
- 1.70.0 cannot store regular files on an S3 endpoint without TLS, such as
  the bundled MinIO (fixed in 1.70.1);
- 1.87.0 and later are licensed under the Business Source License 1.1, not
  Apache 2.0; it works, and whether its terms fit a deployment is the
  deployer's call.

The version is read once (``remember``) and kept for the process.
"""

import re
from dataclasses import dataclass

import httpx

from kohakuhub.logger import get_logger

logger = get_logger("LAKEFS")

MINIMUM = (1, 48, 1)
BROKEN = {
    (1, 48, 0): 'is LakeFS\'s own "do not use" release: it squashes every merge by default',
    (1, 70, 0): "cannot store regular files on an S3 endpoint without TLS",
}
RECOMMENDED = (1, 86, 0)  # the bundled image: the last Apache 2.0 release
TESTED_UP_TO = (1, 88, 0)
FIRST_BSL = (1, 87, 0)
DOCS = "docs/deployment/lakefs.md"

_version: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Assessment:
    version: str | None
    status: str  # supported | unsupported | untested | unknown
    license: str | None  # apache-2.0 | bsl-1.1
    reset_supported: bool
    message: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "license": self.license,
            "reset_supported": self.reset_supported,
            "message": self.message,
        }


def parse(version: str | None) -> tuple[int, int, int] | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _text(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def assess(version: str | None) -> Assessment:
    parsed = parse(version)
    if parsed is None:
        return Assessment(version, "unknown", None, True, "LakeFS version unknown")
    license = "bsl-1.1" if parsed >= FIRST_BSL else "apache-2.0"
    reset_supported = parsed >= MINIMUM
    if parsed in BROKEN:
        status, message = "unsupported", f"LakeFS {_text(parsed)} {BROKEN[parsed]}"
    elif not reset_supported:
        status, message = "unsupported", (
            f"LakeFS {_text(parsed)} is older than {_text(MINIMUM)}: "
            "Reset would leave a merge commit instead of one linear commit, so it is disabled"
        )
    elif parsed > TESTED_UP_TO:
        status, message = "untested", (
            f"LakeFS {_text(parsed)} is newer than the newest tested release, {_text(TESTED_UP_TO)}"
        )
    else:
        status, message = "supported", f"LakeFS {_text(parsed)} is supported"
    if license == "bsl-1.1":
        message += "; it is licensed under the Business Source License 1.1, not Apache 2.0"
    return Assessment(version, status, license, reset_supported, message)


def remember(version: str | None) -> Assessment:
    """Keep the server's version for this process; log what it means."""
    global _version
    result = assess(version)
    parsed = parse(version)
    if parsed is not None and parsed != _version:
        _version = parsed
        if result.status == "unsupported":
            logger.error(f"{result.message}. See {DOCS}.")
        elif result.status == "untested" or result.license == "bsl-1.1":
            logger.warning(f"{result.message}. See {DOCS}.")
        else:
            logger.info(result.message)
    return result


def known() -> Assessment:
    """What is known of the server this process talks to."""
    return assess(_text(_version) if _version else None)


async def learn() -> Assessment:
    """Read the server's version unless known already; never raises.

    A client of its own: the shared pooled one is bound to the event loop
    that first uses it, which need not be the caller's (startup).
    """
    if _version is None:
        from kohakuhub.config import cfg

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{cfg.lakefs.endpoint.rstrip('/')}/api/v1/config/version",
                    auth=(cfg.lakefs.access_key, cfg.lakefs.secret_key),
                )
                response.raise_for_status()
                remember(response.json().get("version"))
        except Exception as e:  # unreachable for now: asked again next time
            logger.info(f"Could not read the LakeFS version yet: {e}")
    return known()
