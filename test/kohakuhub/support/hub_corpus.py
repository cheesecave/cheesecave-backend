"""The frozen sample of real Hub repositories the downstream-library tests read (see the corpus README).

Nothing here talks to the Hub: ``manifest.json`` and the files beside it were sampled once by
``scripts/corpus/sample.py``. A test seeds a repository into the live server under the same files and
asks a library to read it from there; the reference is the same library reading the sampled directory.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "hub_corpus"
NAMESPACE = "owner"


@dataclass(frozen=True)
class CorpusRepo:
    key: str  # "model/<alias>"
    id: str  # the id on the real Hub
    type: str  # "model" | "dataset"
    revision: str
    license: str | None
    files: tuple
    hub: dict  # what the Hub said about it when sampled

    @property
    def alias(self) -> str:
        return self.key.split("/", 1)[1]

    @property
    def server_id(self) -> str:
        return f"{NAMESPACE}/{self.alias}"

    @property
    def archive(self) -> Path:
        return CORPUS_DIR / "repos" / self.type / f"{self.alias}.zip"

    @property
    def directory(self) -> Path:
        """The files of the repository, released from its zip into a temporary directory (once)."""
        return _released(self.archive)

    def paths(self, suffixes=None) -> list[str]:
        return [f["path"] for f in self.files if suffixes is None or f["path"].lower().endswith(tuple(suffixes))]


@lru_cache(maxsize=1)
def _root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="hub-corpus-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    return root


@lru_cache(maxsize=None)
def _released(archive: Path) -> Path:
    target = _root() / hashlib.sha1(str(archive).encode()).hexdigest()[:12] / archive.stem  # one per archive path
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(target)
    return target


@lru_cache(maxsize=1)
def _manifest() -> dict:
    return json.loads((CORPUS_DIR / "manifest.json").read_text())["repos"]


def repos(kind: str) -> list[CorpusRepo]:
    return [
        CorpusRepo(
            key=key,
            id=entry["id"],
            type=entry["type"],
            revision=entry["revision"],
            license=entry.get("license"),
            files=tuple(entry["files"]),
            hub=entry.get("hub") or {},
        )
        for key, entry in sorted(_manifest().items())
        if entry["type"] == kind
    ]


def param_id(repo: CorpusRepo) -> str:
    """A short, stable test id (a long one slows the log pipeline)."""
    return repo.alias[:48]


def verify(repo: CorpusRepo) -> list[str]:
    """What is wrong with the files of ``repo`` on disk: missing, a different size or a different hash."""
    problems = []
    for entry in repo.files:
        path = repo.directory / entry["path"]
        if not path.is_file():
            problems.append(f"missing {entry['path']}")
            continue
        data = path.read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.append(f"changed {entry['path']}")
    return problems


def seed(api, repo: CorpusRepo) -> str:
    """Put ``repo`` into the server the way a user would (one commit), once; the id it has there."""
    from huggingface_hub import CommitOperationAdd

    if _already_there(api, repo):
        return repo.server_id
    api.create_repo(repo.server_id, repo_type=repo.type, exist_ok=True)
    operations = [
        CommitOperationAdd(path_in_repo=entry["path"], path_or_fileobj=(repo.directory / entry["path"]).read_bytes())
        for entry in repo.files
    ]
    api.create_commit(repo.server_id, repo_type=repo.type, operations=operations, commit_message="seed")
    return repo.server_id


def _already_there(api, repo: CorpusRepo) -> bool:
    try:
        return sorted(api.list_repo_files(repo.server_id, repo_type=repo.type)) == sorted(repo.paths())
    except Exception:  # not there yet
        return False
