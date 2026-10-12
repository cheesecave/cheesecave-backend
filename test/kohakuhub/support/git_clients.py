"""Run the real ``git`` and ``git-lfs`` commands against a live server, in a scratch HOME."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def git_available() -> bool:
    return shutil.which("git") is not None


def lfs_available() -> bool:
    if not git_available():
        return False
    return subprocess.run(["git", "lfs", "version"], capture_output=True).returncode == 0


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


class GitClient:
    """A git whose HOME and config are private to one test.

    ``LC_ALL=C`` keeps the messages comparable, ``GIT_TERMINAL_PROMPT=0`` turns
    "git would ask for credentials" into a failure whose message says so.
    """

    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.env = {
            **os.environ,
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        for proxy in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            self.env.pop(proxy, None)
        self.run(["git", "config", "--global", "user.email", "t@example.com"])
        self.run(["git", "config", "--global", "user.name", "t"])
        if lfs_available():
            self.run(["git", "lfs", "install", "--skip-repo"])

    def run(self, command, cwd=None, skip_smudge=False, timeout=180) -> Result:
        env = dict(self.env)
        if skip_smudge:
            env["GIT_LFS_SKIP_SMUDGE"] = "1"
        done = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def clone(self, url, dest, *args, skip_smudge=False) -> Result:
        return self.run(["git", "clone", *args, url, str(dest)], skip_smudge=skip_smudge)


def with_credentials(url: str, username: str, token: str) -> str:
    """``http://host/...`` -> ``http://user:token@host/...`` (what ``hfutils`` builds)."""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{username}:{token}@{rest}"
