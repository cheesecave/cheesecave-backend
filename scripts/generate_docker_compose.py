#!/usr/bin/env python3
"""Copy the standalone Compose template, or create its release/secrets .env.

The former monorepo interactive generator is retired. Infrastructure overrides
are ordinary Compose override files; no frontend bundles are mounted here.
"""

import argparse
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Compose output (default: compose.yml)")
    parser.add_argument("--generate-config", action="store_true", help="Create a local .env")
    args = parser.parse_args()
    output = args.output or Path(".env" if args.generate_config else "compose.yml")
    if output.exists():
        parser.error(
            f"{output} exists; edit it directly instead of overwriting deployment settings"
        )
    if args.generate_config:
        text = (ROOT / ".env.compose.example").read_text(encoding="utf-8")
        for field in ("SESSION_SECRET", "ADMIN_SECRET_TOKEN", "DATABASE_KEY"):
            text = text.replace(
                f"KOHAKU_HUB_{field}=CHANGE_ME", f"KOHAKU_HUB_{field}={secrets.token_hex(32)}"
            )
        for field in ("POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD", "LAKEFS_AUTH_ENCRYPT_SECRET_KEY"):
            text = text.replace(f"{field}=CHANGE_ME", f"{field}={secrets.token_hex(24)}")
        if hasattr(os, "getuid"):
            # LakeFS and Valkey run as UID:GID; Docker would create their mounts as root.
            text = text.replace("\nUID=1000\n", f"\nUID={os.getuid()}\n")
            text = text.replace("\nGID=1000\n", f"\nGID={os.getgid()}\n")
            for name in ("lakefs-data", "lakefs-cache", "valkey-data"):
                (output.parent / "hub-meta" / name).mkdir(parents=True, exist_ok=True)
    else:
        text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    output.write_text(text, encoding="utf-8")
    if args.generate_config:
        output.chmod(0o600)
    print(f"Created {output}. See docs/deployment/docker.md before starting services.")


if __name__ == "__main__":
    main()
