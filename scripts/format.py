#!/usr/bin/env python3
"""Format the standalone backend Python sources with the project line length."""

from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "black",
                "--line-length",
                "100",
                "src",
                "test",
                "scripts",
                "docker/startup.py",
            ],
            cwd=root,
        )
    )
