"""Which data files make which split of which config (layer L0 of the data explorer).

Pure functions, no I/O: given the repository's file list and its README, say what the explorer should show.
The rules are the ones Hugging Face documents for dataset repositories (file names and directory names that
carry ``train``/``validation``/``test`` keywords, ``<split>-00000-of-00003`` shards, and the ``configs`` block
of the README's YAML front matter), written from that public documentation. The same rules are mirrored in the
browser (``web/lib/splits.js``) so a file page can resolve its own split without a round trip.
"""

import fnmatch
import posixpath
import re

import yaml

# keyword -> canonical split name (documented equivalences)
SPLIT_KEYWORDS = {
    "train": "train", "training": "train",
    "validation": "validation", "valid": "validation", "val": "validation", "dev": "validation",
    "test": "test", "testing": "test", "eval": "test", "evaluation": "test",
}
FORMATS = {
    ".parquet": "parquet", ".csv": "csv", ".tsv": "csv", ".jsonl": "jsonl", ".json": "json",
    ".arrow": "arrow", ".txt": "text", ".tar": "webdataset",
}
_SHARD = re.compile(r"^(?P<split>[A-Za-z][A-Za-z0-9_.\-]*?)-\d{3,}-of-\d{3,}$")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def format_of(path: str) -> str | None:
    """The explorer's name for a data file's format, or None when it is not a data file."""
    base = posixpath.basename(path)
    if base.startswith(".") or base.lower() in ("readme.md",):
        return None
    ext = posixpath.splitext(base.lower())[1]
    return FORMATS.get(ext)


def front_matter(readme: str | None) -> dict:
    """The YAML front matter of a README, ``{}`` when there is none or it does not parse."""
    if not readme:
        return {}
    match = _FRONT_MATTER.match(readme)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _tokens(text: str) -> list[str]:
    # a keyword counts only when delimited by non-letters: "train1.csv" and "my_train_file.csv" yes, "testfile.csv" no
    return [t for t in re.split(r"[^A-Za-z]+", text.lower()) if t]


def split_of(path: str) -> str | None:
    """The split a data file belongs to by its name or by a directory above it, or None."""
    directory, base = posixpath.split(path)
    stem = posixpath.splitext(base)[0]
    # data/<custom split>-00000-of-00003.parquet
    if (m := _SHARD.match(stem)):
        name = m.group("split")
        return SPLIT_KEYWORDS.get(name.lower(), name)
    for token in _tokens(stem):
        if token in SPLIT_KEYWORDS:
            return SPLIT_KEYWORDS[token]
    for part in reversed([p for p in directory.split("/") if p]):
        for token in _tokens(part):
            if token in SPLIT_KEYWORDS:
                return SPLIT_KEYWORDS[token]
    return None


def _glob(pattern: str, paths: list[str]) -> list[str]:
    # '**' crosses directories, '*' does not; both are matched against the whole path
    regex = re.compile(
        "^" + re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]") + "$"
    )
    return sorted(p for p in paths if regex.match(p))


def _config_from_yaml(entry: dict, data_paths: list[str]) -> dict:
    prefix = (entry.get("data_dir") or "").strip("/")
    pool = [p for p in data_paths if not prefix or p.startswith(prefix + "/")]
    splits: dict[str, list[str]] = {}
    for item in entry.get("data_files") or []:
        if isinstance(item, str):  # data_files: "x/*.parquet" means one split, train
            item = {"split": "train", "path": item}
        patterns = item.get("path")
        patterns = [patterns] if isinstance(patterns, str) else list(patterns or [])
        split = str(item.get("split", "train"))
        for pattern in patterns:
            pattern = f"{prefix}/{pattern}" if prefix and not pattern.startswith(prefix + "/") else pattern
            splits.setdefault(split, []).extend(f for f in _glob(pattern, pool) if f not in splits.get(split, []))
    return splits


def resolve(files: list[dict], readme: str | None = None) -> dict:
    """What the explorer shows for a repository.

    ``files`` are ``{"path": str, "size": int}``; returns::

        {"source": "yaml" | "naming",
         "configs": [{"config": str, "default": bool,
                      "splits": [{"split": str, "format": str, "files": [{"path", "size"}], "notes": [str]}]}]}
    """
    sizes = {f["path"]: f.get("size", 0) for f in files}
    data_paths = sorted(p for p in sizes if format_of(p))
    meta = front_matter(readme)
    yaml_configs = meta.get("configs")
    configs = []
    if isinstance(yaml_configs, list) and yaml_configs:
        source = "yaml"
        entries = [c for c in yaml_configs if isinstance(c, dict)]
        explicit = [bool(c.get("default")) for c in entries]
        for i, entry in enumerate(entries):
            by_split = _config_from_yaml(entry, data_paths)
            # an explicit ``default: true`` wins; without one the first config is the default
            default = explicit[i] if any(explicit) else i == 0
            configs.append({"config": str(entry.get("config_name", "default")), "default": default, "by_split": by_split})
    else:
        source = "naming"
        by_split: dict[str, list[str]] = {}
        for path in data_paths:
            by_split.setdefault(split_of(path) or "train", []).append(path)
        if by_split:
            configs.append({"config": "default", "default": True, "by_split": by_split})
    out = []
    for config in configs:
        splits = []
        for name, paths in config["by_split"].items():
            formats = sorted({format_of(p) for p in paths})
            notes = []
            if len(formats) > 1:
                notes.append(f"mixed formats {formats}: the explorer reads the first ({formats[0]})")
            fmt = formats[0] if formats else "unknown"
            usable = [p for p in paths if format_of(p) == fmt]
            splits.append({"split": name, "format": fmt, "files": [{"path": p, "size": sizes[p]} for p in usable], "notes": notes})
        out.append({"config": config["config"], "default": config["default"], "splits": splits})
    return {"source": source, "configs": out}
