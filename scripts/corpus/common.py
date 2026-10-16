"""Shared bits of the one-off corpus sampling (see scripts/corpus/README.md).

The sampling is run by hand, slowly, against the real Hub; CI never runs it.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path, PurePosixPath

# What is kept of a repository: text, tokenizer, table and index files. Weights and every other binary
# format are never kept (a safetensors file is kept as its header only), so the list is an allow-list.
KEEP_SUFFIXES = {
    ".json", ".txt", ".md", ".model", ".tiktoken", ".yaml", ".yml", ".jinja", ".vocab", ".merges",
    ".parquet", ".csv", ".tsv", ".jsonl", ".ndjson",
}
KEEP_NAMES = {"merges.txt", "vocab.txt", "vocab.json", "spiece.model", "sentencepiece.bpe.model", "tokenizer.model"}
MAX_FILE = 12 * 1024 * 1024  # a practical ceiling on one kept file, not a rule about formats
PAUSE = 0.1  # seconds between requests: the Hub is a guest here


def keep(path: str) -> bool:
    p = PurePosixPath(path)
    return p.name in KEEP_NAMES or p.suffix.lower() in KEEP_SUFFIXES


def is_safetensors(path: str) -> bool:
    return path.endswith(".safetensors")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pause() -> None:
    time.sleep(PAUSE)


def pack(directory: Path, archive: Path) -> None:
    """The files under ``directory`` as one zip, byte for byte reproducible (sorted, fixed dates)."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, path.read_bytes())
