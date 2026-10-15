"""What a model repository's files say about it: library, task, framework tags, config.

The Hub fills ``library_name``, ``pipeline_tag``, the framework tags and ``config`` from the
files when the README does not. Every rule here was tuned against the Hub's own answers for
hundreds of real repositories, and the principle is that a value is returned only when it is
right: a rule that would be wrong for one repository in twenty (``config.json`` plus weights
meaning ``transformers`` is one, for repositories that other libraries also write) is not a
rule. Nothing here reads the network or the database; ``names`` is the root and sub-directory
file list and ``files`` maps the few small JSON files that were read to their parsed values.
"""

from __future__ import annotations

import json
import math
import re

from kohakuhub import repo_card

# Bump when a rule changes: every stored row made by an older version is derived again.
VERSION = 2

# Libraries a card may declare in its tags; the Hub takes the first one as the library.
DECLARED_LIBRARIES = frozenset({
    "spacy", "flair", "speechbrain", "asteroid", "pyannote-audio", "espnet", "fastai", "sklearn",
    "mlx", "ml-agents", "keras", "paddlenlp", "stable-baselines3", "nemo", "bertopic", "timm",
    "open_clip", "peft", "diffusers", "sentence-transformers", "setfit", "adapter-transformers",
    "fairseq", "joblib", "tf-keras", "skops", "transformers.js", "gguf", "diffusion-single-file",
    "stanza", "allennlp", "sample-factory", "rl-baselines3-zoo", "pythae", "fasttext", "span-marker",
})
# Tags and files that mean "not (only) transformers" even next to config.json and weights
OTHER_LIBRARY_TAGS = DECLARED_LIBRARIES | {"onnx"}
OTHER_LIBRARY_FILES = frozenset({
    "model_state.pdparams", "export.pkl", "hyperparams.yaml", "meta.json", "model.nemo",
})
SENTENCE_TRANSFORMERS_FILES = frozenset({
    "modules.json", "config_sentence_transformers.json", "sentence_bert_config.json",
})
MARKER_FILES = frozenset({"model_index.json", "modules.json", "open_clip_config.json"})
TRANSFORMERS_WEIGHTS = re.compile(
    r"^(pytorch_model(-\d+-of-\d+)?\.bin|model(-\d+-of-\d+)?\.safetensors|tf_model(-\d+-of-\d+)?\.h5"
    r"|flax_model(-\d+-of-\d+)?\.msgpack|rust_model\.ot|pytorch_model\.bin\.index\.json"
    r"|model\.safetensors\.index\.json)$"
)
# The tasks a card names in its tags (the Hub takes a lone one as the task)
TASK_TAGS = frozenset({
    "text-classification", "token-classification", "fill-mask", "automatic-speech-recognition",
    "image-classification", "object-detection", "text-to-image", "translation", "summarization",
    "question-answering", "feature-extraction", "text2text-generation", "audio-classification",
    "zero-shot-classification", "image-to-text", "text-to-speech", "depth-estimation", "image-segmentation",
    "sentence-similarity", "visual-question-answering", "text-generation", "reinforcement-learning",
    "tabular-classification", "image-feature-extraction", "voice-activity-detection", "audio-to-audio",
    "text-to-video", "image-text-to-text", "any-to-any", "video-classification",
    "zero-shot-image-classification", "conversational", "table-question-answering", "text-ranking",
    "robotics", "mask-generation", "video-text-to-text", "unconditional-image-generation", "image-to-image",
    "image-to-video", "text-to-3d", "image-to-3d", "document-question-answering",
    "zero-shot-object-detection", "tabular-regression", "time-series-forecasting", "audio-text-to-text",
    "keypoint-detection", "visual-document-retrieval", "multiple-choice", "graph-ml", "text-retrieval",
})
# The head class names the task only where it is unambiguous: ForSequenceClassification is shared
# with audio models. ForCausalLM is text-generation unless the card says otherwise (translation
# models are ForCausalLM too, and say so in their tags).
HEAD_TASKS = (
    ("ForMaskedLM", "fill-mask"),
    ("ForTokenClassification", "token-classification"),
    ("ForQuestionAnswering", "question-answering"),
    ("ForImageClassification", "image-classification"),
    ("ForAudioClassification", "audio-classification"),
    ("ForObjectDetection", "object-detection"),
    ("ForSemanticSegmentation", "image-segmentation"),
    ("ForCTC", "automatic-speech-recognition"),
    ("ForSpeechSeq2Seq", "automatic-speech-recognition"),
)
CAUSAL_TASK = ("ForCausalLM", "text-generation")
SPECIAL_TOKENS = (
    "bos_token", "eos_token", "pad_token", "unk_token", "cls_token", "sep_token", "mask_token",
    "chat_template", "use_default_system_prompt",
)
# (tag, does one of the file names say so)
FRAMEWORKS = (
    ("pytorch", re.compile(r"^pytorch_model(-\d+-of-\d+)?\.bin$")),
    ("tf", re.compile(r"^tf_model(-\d+-of-\d+)?\.h5$")),
    ("jax", re.compile(r"^flax_model(-\d+-of-\d+)?\.msgpack$")),
    ("rust", re.compile(r"^rust_model\.ot$")),
    ("onnx", re.compile(r"\.onnx$")),
    (
        "safetensors",
        re.compile(
            r"^(model|pytorch_model|adapter_model|open_clip_model)(\.safetensors)?(-\d+-of-\d+)?\.safetensors$"
            r"|^(model|pytorch_model)\.safetensors-\d+-of-\d+\.safetensors$"
            r"|^(.*/)?diffusion_pytorch_model(\.fp16)?\.safetensors$"
        ),
    ),
    ("tflite", re.compile(r"\.tflite$")),
    ("coreml", re.compile(r"\.mlmodel$|\.mlpackage/")),
    ("openvino", re.compile(r"^openvino_model\.xml$|^openvino/")),
    ("gguf", re.compile(r"\.gguf$")),
)
# Libraries whose config.json model_type the Hub also lists as a tag
MODEL_TYPE_LIBRARIES = {"transformers", "sentence-transformers", "open_clip", None}


def _dict(value) -> dict | None:
    return value if isinstance(value, dict) else None


def _card_tags(card: dict) -> list[str]:
    tags = card.get("tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def _library(card: dict, names: set[str], files: dict) -> str | None:
    declared_name = card.get("library_name")
    if isinstance(declared_name, str) and declared_name:
        return declared_name
    tags = _card_tags(card)
    declared = [t for t in tags if t in DECLARED_LIBRARIES]
    if len(set(declared)) > 1:
        return None  # the tags disagree: say nothing rather than guess
    if declared == ["setfit"] and "config_setfit.json" not in names:
        return None  # the Hub reports sentence-transformers for many of these
    if declared == ["transformers.js"]:
        return None  # the Hub reports transformers for these
    if declared and not (MARKER_FILES & names):
        return declared[0]
    if "model_index.json" in names:
        return "diffusers"
    if "open_clip_config.json" in names:
        return "open_clip"
    if SENTENCE_TRANSFORMERS_FILES & names:
        return "sentence-transformers"
    adapter = _dict(files.get("adapter_config.json"))
    if "adapter_config.json" in names and adapter and "peft_type" in adapter:
        return "peft"
    config = _dict(files.get("config.json"))
    root_weights = any(TRANSFORMERS_WEIGHTS.match(n) for n in names if "/" not in n)
    if config and ("model_type" in config or "architectures" in config) and root_weights:
        if not (set(tags) & OTHER_LIBRARY_TAGS) and not (names & OTHER_LIBRARY_FILES):
            return "transformers"
    return None


def _written_task(card: dict) -> str | None:
    """The task as the author wrote it: ``pipeline_tag``, else the one task named in the tags."""
    declared = card.get("pipeline_tag")
    if isinstance(declared, str):
        return declared
    named = {t for t in _card_tags(card) if t in TASK_TAGS}
    return next(iter(named)) if len(named) == 1 else None


def _pipeline(card: dict, library: str | None, files: dict) -> str | None:
    declared = repo_card.declared(card)["pipeline_tag"]
    if declared:
        return declared
    named = {t for t in _card_tags(card) if t in TASK_TAGS}
    if len(named) == 1:  # a lone task in the tags is the task
        return repo_card.MERGED_TASKS.get(task := named.pop(), task)
    config = _dict(files.get("config.json"))
    if library == "transformers" and config:
        head = (config.get("architectures") or [None])[0]
        if isinstance(head, str):
            for suffix, task in HEAD_TASKS:
                if head.endswith(suffix):
                    return task
            if head.endswith(CAUSAL_TASK[0]) and not named:  # two task tags: no telling which
                return CAUSAL_TASK[1]
    return None


def _tags(library: str | None, task: str | None, names: list[str], files: dict) -> list[str]:
    tags = [library] if library else []
    tags += [tag for tag, pattern in FRAMEWORKS if any(pattern.search(n) for n in names)]
    config = _dict(files.get("config.json"))
    if config and isinstance(config.get("model_type"), str) and library in MODEL_TYPE_LIBRARIES:
        tags.append(config["model_type"])
    if task:
        tags.append(task)
    index = _dict(files.get("model_index.json"))
    if index and isinstance(index.get("_class_name"), str):
        tags.append("diffusers:" + index["_class_name"])
    return list(dict.fromkeys(tags))


def _config(files: dict) -> dict | None:
    """The part of ``config`` that is exact: keys the Hub also shows are copied only when they
    were the same for every sampled repository (the rest differ with the model)."""
    config = _dict(files.get("config.json"))
    index = _dict(files.get("model_index.json"))
    if config is None:
        if index and isinstance(index.get("_class_name"), str):
            return {"diffusers": {"_class_name": index["_class_name"]}}
        return None
    out = {k: config[k] for k in ("architectures", "model_type", "auto_map") if k in config}
    if isinstance(out.get("auto_map"), dict):
        out["auto_map"] = {k: v for k, v in out["auto_map"].items() if k}
    tokenizer = _dict(files.get("tokenizer_config.json"))
    if tokenizer is not None:  # one that could not be read adds nothing: the Hub lists its tokens
        out["tokenizer_config"] = {k: tokenizer[k] for k in SPECIAL_TOKENS if k in tokenizer}
    return out


def derive(card: dict, names: list[str], files: dict) -> dict:
    """``{"library_name", "pipeline_tag", "tags", "config"}`` for one model repository."""
    name_set = set(names)
    library = _library(card, name_set, files)
    pipeline = _pipeline(card, library, files)
    written = _written_task(card)  # tags keep the author's word
    return {
        "library_name": library,
        "pipeline_tag": pipeline,
        "tags": _tags(library, written or pipeline, names, files),
        "config": _config(files),
    }


def merge_tags(card_tags: list[str], card: dict, derived: dict) -> list[str]:
    """The tags of a repository in the Hub's order: library, what the files say (frameworks,
    model type), the task, the pipeline class of a diffusers repository, then what the card says
    (``card_tags`` from ``repo_card.compose_tags``, which ends with ``region:us``). The order is
    what the Hub does for 63 of 485 sampled repositories exactly and for 344 as a subsequence;
    clients read tags as a set."""
    library = derived["library_name"]
    task = _written_task(card) or derived["pipeline_tag"]
    classes = [t for t in derived["tags"] if t.startswith("diffusers:")]
    filed = [t for t in derived["tags"] if t not in {library, task, *classes}]
    head = [library] if library else []
    return list(dict.fromkeys(head + filed + ([task] if task else []) + classes + card_tags))


# ---------- the safetensors summary ----------
#
# The Hub answers ``safetensors: {"parameters": {dtype: count}, "total": n}`` for a model whose
# weights are a ``model.safetensors`` or ``model-0000i-of-0000N.safetensors`` in the root. The rules
# were checked against the real Hub's answer for 172 repositories (147 answered, all equal; the rest
# are left null): quantized models (the Hub counts what the model has once unpacked, the stored
# tensors say less), repositories with two sets of weights, diffusers pipelines (the Hub sums a
# component of its own choosing) and anything unreadable are not summed.

MAX_SAFETENSORS_HEADER = 4 * 1024 * 1024  # the JSON header of a file; the library itself allows 100 MB
SAFETENSORS_SINGLE = "model.safetensors"
SAFETENSORS_SHARD = re.compile(r"^model-(\d{5})-of-(\d{5})\.safetensors$")
PACKED_DTYPE = re.compile(r"^(U32|U8|I8|I4|U4|F8_.*|F4.*)$")


def weight_files(names: list[str]) -> list[str] | None:
    """The one complete set of weights in the root (sorted), or ``None`` when there is none or
    it is not clear which one is the model."""
    root = [n for n in names if re.match(r"^model[^/]*\.safetensors$", n)]
    single = [n for n in root if n == SAFETENSORS_SINGLE]
    shards = sorted(n for n in root if SAFETENSORS_SHARD.match(n))
    if len(single) + len(shards) != len(root) or bool(single) == bool(shards):
        return None  # something else in the root, two sets, or nothing
    if single:
        return single
    totals = {SAFETENSORS_SHARD.match(n).group(2) for n in shards}
    if len(totals) != 1 or len(shards) != int(next(iter(totals))):
        return None  # shards of two sets, or one is missing
    return shards


def safetensors_header_size(prefix: bytes) -> int | None:
    """The length of the JSON header, from the first 8 bytes of a file; ``None`` when it is not
    there or implausible."""
    if len(prefix) < 8:
        return None
    size = int.from_bytes(prefix[:8], "little")
    return size if 0 < size <= MAX_SAFETENSORS_HEADER else None


def parse_safetensors_header(blob: bytes) -> dict | None:
    """``{tensor: {"dtype", "shape"}}`` from the length and the header of a file (``__metadata__``
    dropped), or ``None`` when it is not a safetensors header."""
    size = safetensors_header_size(blob)
    if size is None or len(blob) < 8 + size:
        return None
    try:
        raw = json.loads(blob[8 : 8 + size])
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    raw.pop("__metadata__", None)
    out = {}
    for name, entry in raw.items():
        shape = entry.get("shape") if isinstance(entry, dict) else None
        dtype = entry.get("dtype") if isinstance(entry, dict) else None
        if not isinstance(dtype, str) or not isinstance(shape, list):
            return None
        if any(type(d) is not int or d < 0 for d in shape):
            return None
        out[name] = {"dtype": dtype, "shape": shape}
    return out


def is_quantized(config) -> bool:
    return isinstance(config, dict) and ("quantization" in config or "quantization_config" in config)


def safetensors_summary(files: list[str], headers: dict, quantized: bool) -> dict | None:
    """``{"parameters": {dtype: count}, "total": n}`` over the ``files`` (their parsed headers in
    ``headers``; ``None`` for one that could not be read), or ``None`` when it would not be what
    the Hub answers."""
    if quantized or any(headers.get(f) is None for f in files):
        return None
    parameters: dict[str, int] = {}
    for f in files:
        for tensor in headers[f].values():
            if not tensor["shape"]:
                continue  # a tensor without dimensions is not counted
            count = math.prod(tensor["shape"])
            parameters[tensor["dtype"]] = parameters.get(tensor["dtype"], 0) + count
    total = sum(parameters.values())
    if not total or any(PACKED_DTYPE.match(d) for d in parameters):
        return None
    return {"parameters": parameters, "total": total}
