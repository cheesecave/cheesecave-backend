"""A repository card: README front matter -> the Hub's ``cardData`` and the fields it declares.

The front matter is YAML that anybody can write, and ``huggingface_hub`` builds its info
objects from what we answer, so one odd README must never break every ``model_info`` call:
keys become strings, ``tags`` is always a list of strings, dates and NaN become JSON, and an
oversized card drops its biggest values. Aliases, deep nesting and huge node counts are refused
while parsing. Nothing here reads the network or the database.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime

import yaml

MAX_CARD_BYTES = 262144  # the README prefix that is read
MAX_NODES = 100_000
MAX_DEPTH = 24
MAX_CARD_JSON = 1_048_576  # what a card may weigh once it is JSON
_VALUE_DEPTH = 32


class CardLoader(yaml.SafeLoader):
    """No aliases, recursive objects or unbounded YAML node expansion."""

    def compose_node(self, parent, index):
        self.card_nodes = getattr(self, "card_nodes", 0) + 1
        self.card_depth = getattr(self, "card_depth", 0) + 1
        if self.card_nodes > MAX_NODES or self.card_depth > MAX_DEPTH or self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("Card metadata exceeds parsing limits")
        try:
            return super().compose_node(parent, index)
        finally:
            self.card_depth -= 1


# Only true/false are booleans (yes/no/on/off stay text), as the Hub reads them
CardLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
CardLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


def _front_matter_text(content: bytes | str) -> str | None:
    """The text between the first two ``---`` lines, or ``None`` when there is no closed block."""
    if isinstance(content, str):
        content = content.encode()
    lines = content[:MAX_CARD_BYTES].decode("utf-8-sig", errors="replace").splitlines()
    while lines and not lines[0].strip():  # the Hub skips blank lines before the opening marker
        lines.pop(0)
    if not lines or lines[0].strip() != "---":
        return None
    closing = next((n for n, line in enumerate(lines[1:], 1) if line.rstrip() in {"---", "..."}), None)
    return None if closing is None else "\n".join(lines[1:closing])


def load_front_matter(content: bytes) -> dict | None:
    """The YAML mapping between the first two ``---`` lines, or ``None``."""
    text = _front_matter_text(content)
    if text is None:
        return None
    try:
        raw = yaml.load(text, Loader=CardLoader)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) else None


CARD_HELP = "https://huggingface.co/docs/hub/model-cards#model-card-metadata"


def check_readme(content: str) -> tuple[list[dict], list[dict]]:
    """What ``POST /api/validate-yaml`` answers for a README: ``(errors, warnings)``, as the Hub
    words them. Only the structure is checked: the Hub's lists of licenses, languages and
    tasks are its own."""
    text = _front_matter_text(content)
    raw = None
    if text is not None:
        try:
            raw = yaml.load(text, Loader=CardLoader)
        except (yaml.YAMLError, ValueError, RecursionError) as error:
            first = str(error).splitlines()[0] if str(error) else type(error).__name__
            return [{"message": f"Invalid YAML in README.md: {first}", "path": []}], []
        if raw is not None and not isinstance(raw, dict):
            return [{"message": '"value" must be of type object', "path": []}], []
    if not raw:
        return [], [{"message": "empty or missing yaml metadata in repo card", "help": CARD_HELP}]
    return [], []


_DROP = object()


def _safe(value, depth=0):
    """``value`` as JSON can carry it; ``_DROP`` for what it cannot."""
    if depth > _VALUE_DEPTH:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = _safe(item, depth + 1)
            if item is not _DROP:
                out[str(key)] = item
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [item for item in (_safe(i, depth + 1) for i in items) if item is not _DROP]
    return _DROP


def _unverify(index) -> None:
    """The Hub never takes a README's word that an evaluation result is verified: every metric says so."""
    if not isinstance(index, list):
        return
    for entry in index:
        for result in entry.get("results", []) if isinstance(entry, dict) and isinstance(entry.get("results"), list) else []:
            metrics = result.get("metrics") if isinstance(result, dict) else None
            for metric in metrics if isinstance(metrics, list) else []:
                if isinstance(metric, dict):
                    metric["verified"] = False


def clean_card(raw) -> dict | None:
    """``raw`` as the ``cardData`` to answer, or ``None`` for a card with nothing in it."""
    if not isinstance(raw, dict):
        return None
    card = _safe(raw)
    card.pop("ignore_metadata_errors", None)
    _unverify(card.get("model-index"))
    if "tags" in card:
        tags = card["tags"]
        if isinstance(tags, str):
            card["tags"] = [tags]
        elif isinstance(tags, list):
            card["tags"] = [str(t) for t in tags if isinstance(t, (str, int, float)) and not isinstance(t, bool)]
        else:
            del card["tags"]
    while card and len(json.dumps(card)) > MAX_CARD_JSON:
        del card[max(card, key=lambda k: len(json.dumps(card[k])))]
    return card or None


# Tasks the Hub folded into another: it answers the new name as the field and keeps the author's
# words in the tags (seen on real cards: text2text-generation x5, conversational x1)
MERGED_TASKS = {"text2text-generation": "text-generation", "conversational": "text-generation"}


def declared(card: dict | None) -> dict:
    """The few card values the info routes answer as fields of their own."""
    card = card or {}

    def text(name):
        value = card.get(name)
        return value if isinstance(value, str) and value else None

    task = text("pipeline_tag")
    return {"pipeline_tag": MERGED_TASKS.get(task, task), "library_name": text("library_name"), "sdk": text("sdk")}


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


_DATASET_GROUPS = (
    "task_categories",
    "task_ids",
    "annotations_creators",
    "language_creators",
    "multilinguality",
    "source_datasets",
    "language",
    "license",
)  # size_categories is left out: the Hub measures the data and ignores what a card declares
_RELATIONS = {"adapter", "merge", "quantized", "finetune"}


def compose_tags(card: dict | None, repo_type: str) -> list[str]:
    """The ``tags`` a card declares, in the Hub's order. Tags the Hub derives from the files or
    computes in its own services are not here."""
    card = card or {}
    tags: list[str] = []
    free = _strings(card.get("tags"))
    if repo_type == "model":
        for name in ("library_name", "pipeline_tag"):
            tags += _strings(card.get(name))
        tags += free
        tags += _strings(card.get("language"))
        tags += ["dataset:" + d for d in _strings(card.get("datasets"))]
        relation = card.get("base_model_relation")
        for base in _strings(card.get("base_model")):
            tags.append("base_model:" + base)
            if isinstance(relation, str) and relation in _RELATIONS:
                tags.append(f"base_model:{relation}:{base}")
        tags += ["license:" + lic.lower() for lic in _strings(card.get("license"))]
        tags.append("region:us")
    elif repo_type == "dataset":
        for group in _DATASET_GROUPS:
            values = _strings(card.get(group))
            tags += [f"{group}:{v.lower() if group == 'license' else v}" for v in values]
        tags += [t for t in free if ":" in t]
        tags.append("region:us")
        tags += [t for t in free if ":" not in t]
    else:
        tags += _strings(card.get("sdk"))
        tags += free
        tags.append("region:us")
    return list(dict.fromkeys(tags))
