"""What a model repository's files say about it: library, task, framework tags, config.

The rules were tuned against the real Hub's answers (see test_repo_derive_oracle.py). The
principle is that a value is returned only when it is right: no rule fires on a guess.
"""

import pytest

from kohakuhub.repo_derive import derive, merge_tags
from kohakuhub.repo_card import compose_tags

TRANSFORMERS_FILES = ["config.json", "model.safetensors", "tokenizer_config.json"]
BERT = {"config.json": {"model_type": "bert", "architectures": ["BertForMaskedLM"]}}


def compose(card, names, files=None):
    """The tags of a model repository as the info route lists them."""
    return merge_tags(compose_tags(card, "model"), card, derive(card, list(names), files or {}))


def go(card=None, names=(), files=None):
    return derive(card or {}, list(names), files or {})


# ---------- library_name ----------


def test_the_card_wins_over_the_files():
    assert go({"library_name": "timm"}, TRANSFORMERS_FILES, BERT)["library_name"] == "timm"


def test_a_transformers_repository_is_recognised_by_config_and_weights():
    out = go({}, TRANSFORMERS_FILES, BERT)
    assert out["library_name"] == "transformers"
    assert out["pipeline_tag"] == "fill-mask"
    assert out["tags"] == ["transformers", "safetensors", "bert", "fill-mask"]


@pytest.mark.parametrize(
    "names, files, expected",
    [
        (["model_index.json"], {"model_index.json": {"_class_name": "StableDiffusionPipeline"}}, "diffusers"),
        (["open_clip_config.json"], {}, "open_clip"),
        (["modules.json"], {}, "sentence-transformers"),
        (["config_sentence_transformers.json"], {}, "sentence-transformers"),
        (["adapter_config.json"], {"adapter_config.json": {"peft_type": "LORA"}}, "peft"),
    ],
)
def test_marker_files_name_the_library(names, files, expected):
    assert go({}, names, files)["library_name"] == expected


def test_an_adapter_config_without_peft_type_says_nothing():
    assert go({}, ["adapter_config.json"], {"adapter_config.json": {}})["library_name"] is None
    assert go({}, ["adapter_config.json"], {})["library_name"] is None


def test_a_library_declared_in_the_card_tags_is_taken():
    assert go({"tags": ["spacy", "text"]}, ["x.whl"])["library_name"] == "spacy"


def test_a_declared_library_loses_to_a_marker_file():
    out = go({"tags": ["sentence-transformers"]}, ["modules.json"])
    assert out["library_name"] == "sentence-transformers"


def test_tags_that_name_two_libraries_leave_it_open():
    assert go({"tags": ["mlx", "gguf"]}, ["a.gguf"])["library_name"] is None


def test_setfit_needs_its_file():
    assert go({"tags": ["setfit"]}, ["config.json"])["library_name"] is None
    assert go({"tags": ["setfit"]}, ["config_setfit.json"])["library_name"] == "setfit"


def test_transformers_js_is_left_to_the_hub():
    assert go({"tags": ["transformers.js"]}, TRANSFORMERS_FILES, BERT)["library_name"] is None


@pytest.mark.parametrize("tags", ["spacy", None, 3, {"a": 1}])
def test_a_malformed_tags_field_is_ignored(tags):
    assert go({"tags": tags}, ["config.json"])["library_name"] is None


def test_other_library_tags_keep_config_and_weights_from_meaning_transformers():
    out = go({"tags": ["onnx"]}, TRANSFORMERS_FILES, BERT)
    assert out["library_name"] is None


def test_other_library_files_do_too():
    out = go({}, TRANSFORMERS_FILES + ["hyperparams.yaml"], BERT)
    assert out["library_name"] is None


def test_weights_in_a_subdirectory_are_not_enough():
    assert go({}, ["config.json", "sub/model.safetensors"], BERT)["library_name"] is None


def test_a_config_that_is_not_a_model_config_is_not_enough():
    assert go({}, TRANSFORMERS_FILES, {"config.json": {"x": 1}})["library_name"] is None
    assert go({}, TRANSFORMERS_FILES, {"config.json": ["bert"]})["library_name"] is None


# ---------- pipeline_tag ----------


def test_the_card_task_wins():
    assert go({"pipeline_tag": "translation"}, TRANSFORMERS_FILES, BERT)["pipeline_tag"] == "translation"


def test_a_task_the_hub_merged_is_answered_as_the_hub_does():
    out = go({"pipeline_tag": "conversational"})
    assert out["pipeline_tag"] == "text-generation"
    assert out["tags"] == ["conversational"]  # the tag keeps the author's word


@pytest.mark.parametrize(
    "arch, task",
    [
        ("BertForTokenClassification", "token-classification"),
        ("BertForQuestionAnswering", "question-answering"),
        ("ViTForImageClassification", "image-classification"),
        ("Wav2Vec2ForCTC", "automatic-speech-recognition"),
    ],
)
def test_the_head_class_names_the_task(arch, task):
    out = go({}, TRANSFORMERS_FILES, {"config.json": {"model_type": "x", "architectures": [arch]}})
    assert out["pipeline_tag"] == task


@pytest.mark.parametrize(
    "arch",
    [
        "BertForSequenceClassification",  # text or audio
        "BertModel",
    ],
)
def test_ambiguous_heads_name_no_task(arch):
    out = go({}, TRANSFORMERS_FILES, {"config.json": {"model_type": "x", "architectures": [arch]}})
    assert out["pipeline_tag"] is None


LLAMA = {"config.json": {"model_type": "llama", "architectures": ["LlamaForCausalLM"]}}


def test_a_causal_language_model_generates_text():
    assert go({}, TRANSFORMERS_FILES, LLAMA)["pipeline_tag"] == "text-generation"


@pytest.mark.parametrize("tags", [["translation"], ["summarization"], ["translation", "summarization"], ["text-classification"]])
def test_a_card_tag_that_names_a_task_stops_a_causal_model_from_being_called_text_generation(tags):
    """Translation models are ForCausalLM too: tencent/Hunyuan-MT-7B says ``translation`` in its tags. With
    one such tag that is the task; with two there is no telling, so none."""
    out = go({"tags": tags}, TRANSFORMERS_FILES, LLAMA)["pipeline_tag"]

    assert out == (tags[0] if len(tags) == 1 else None)


def test_a_single_card_tag_that_names_a_task_is_the_task_whatever_the_files_say():
    assert go({"tags": ["fill-mask", "x"]}, TRANSFORMERS_FILES, LLAMA)["pipeline_tag"] == "fill-mask"
    assert go({"tags": ["fill-mask"]})["pipeline_tag"] == "fill-mask"  # even without files
    assert go({"tags": ["fill-mask", "fill-mask"]})["pipeline_tag"] == "fill-mask"  # a repeat is one


def test_a_task_tag_the_hub_merged_is_answered_as_it_does():
    assert go({"tags": ["text2text-generation"]})["pipeline_tag"] == "text-generation"


def test_a_merged_task_tag_keeps_the_authors_word_in_the_tags_and_adds_no_other():
    out = go({"tags": ["text2text-generation"]})

    assert out["pipeline_tag"] == "text-generation" and out["tags"] == ["text2text-generation"]


def test_the_card_task_still_wins_over_a_task_tag():
    assert go({"pipeline_tag": "summarization", "tags": ["translation"]})["pipeline_tag"] == "summarization"


def test_no_task_from_a_non_string_architecture():
    out = go({}, TRANSFORMERS_FILES, {"config.json": {"model_type": "x", "architectures": [3]}})
    assert out["pipeline_tag"] is None


def test_no_task_outside_transformers():
    out = go({"library_name": "timm"}, TRANSFORMERS_FILES, BERT)
    assert out["pipeline_tag"] is None


# ---------- tags ----------


def test_framework_tags_follow_the_weight_files():
    names = [
        "pytorch_model.bin", "tf_model.h5", "flax_model.msgpack", "rust_model.ot", "a.onnx",
        "model.safetensors", "a.tflite", "a.mlmodel", "b.mlpackage/Manifest.json",
        "openvino_model.xml", "a.gguf",
    ]
    assert go({}, names)["tags"] == [
        "pytorch", "tf", "jax", "rust", "onnx", "safetensors", "tflite", "coreml", "openvino", "gguf",
    ]


def test_loose_pickles_do_not_make_a_pytorch_tag():
    assert go({}, ["w.pt", "w.pth", "w.ckpt"])["tags"] == []


def test_sharded_weights_count():
    assert "safetensors" in go({}, ["model-00001-of-00002.safetensors"])["tags"]
    assert "safetensors" in go({}, ["model.safetensors-00001-of-00002.safetensors"])["tags"]
    assert "safetensors" in go({}, ["unet/diffusion_pytorch_model.fp16.safetensors"])["tags"]
    assert "pytorch" in go({}, ["pytorch_model-00001-of-00002.bin"])["tags"]


def test_a_diffusers_repository_gets_its_pipeline_class_tag():
    out = go({}, ["model_index.json"], {"model_index.json": {"_class_name": "FluxPipeline"}})
    assert out["tags"] == ["diffusers", "diffusers:FluxPipeline"]


def test_a_model_index_without_a_class_adds_no_class_tag():
    out = go({}, ["model_index.json"], {"model_index.json": {}})
    assert out["tags"] == ["diffusers"]


def test_tags_are_unique_and_ordered():
    out = go({"pipeline_tag": "fill-mask"}, TRANSFORMERS_FILES + ["pytorch_model.bin"], BERT)
    assert out["tags"] == ["transformers", "pytorch", "safetensors", "bert", "fill-mask"]


def test_the_model_type_is_a_tag_for_a_few_libraries_only():
    cfg = {"config.json": {"model_type": "resnet"}}
    assert "resnet" not in go({"library_name": "timm"}, ["config.json"], cfg)["tags"]
    assert "resnet" in go({"library_name": "sentence-transformers"}, ["config.json"], cfg)["tags"]


# ---------- config ----------


def test_the_config_keeps_its_stable_keys():
    cfg = {"model_type": "bert", "architectures": ["BertModel"], "hidden_size": 8, "auto_map": {"": "a.B", "AutoModel": "m.M"}}
    out = go({}, ["config.json"], {"config.json": cfg})["config"]
    assert out == {"model_type": "bert", "architectures": ["BertModel"], "auto_map": {"AutoModel": "m.M"}}


def test_a_tokenizer_config_adds_its_special_tokens():
    tk = {"bos_token": "<s>", "model_max_length": 5, "chat_template": "t"}
    out = go({}, ["config.json", "tokenizer_config.json"], {"config.json": BERT["config.json"], "tokenizer_config.json": tk})["config"]
    assert out["tokenizer_config"] == {"bos_token": "<s>", "chat_template": "t"}


def test_a_tokenizer_config_that_cannot_be_read_adds_nothing():
    """Not an empty entry: real repositories whose tokenizer_config.json is too big to read
    (whisper's) have their special tokens in the Hub's answer, and ``{}`` would be a wrong one."""
    out = go({}, ["config.json", "tokenizer_config.json"], {"config.json": BERT["config.json"]})["config"]
    assert "tokenizer_config" not in out


def test_a_diffusers_repository_without_config_json_reports_its_class():
    out = go({}, ["model_index.json"], {"model_index.json": {"_class_name": "FluxPipeline"}})["config"]
    assert out == {"diffusers": {"_class_name": "FluxPipeline"}}


def test_no_config_without_a_readable_one():
    assert go({}, ["config.json"])["config"] is None
    assert go({}, ["model_index.json"], {"model_index.json": {}})["config"] is None
    assert go({}, [])["config"] is None


# ---------- the tags of a repository: what the files say, then what the card says ----------


def test_the_order_is_the_hubs_library_frameworks_task_then_the_card():
    card = {"tags": ["mteb"], "language": "en", "license": "mit", "pipeline_tag": "feature-extraction"}
    names = ["modules.json", "model.safetensors", "pytorch_model.bin"]
    cfg = {"config.json": {"model_type": "bert"}}

    tags = compose(card, names, cfg)

    assert tags == [
        "sentence-transformers", "pytorch", "safetensors", "bert", "feature-extraction",
        "mteb", "en", "license:mit", "region:us",
    ]


def test_a_card_without_files_keeps_the_order_it_had():
    card = {"library_name": "timm", "pipeline_tag": "image-classification", "tags": ["a"]}

    assert compose(card, []) == ["timm", "image-classification", "a", "region:us"]


def test_a_repository_without_a_card_gets_the_derived_tags_and_the_region():
    assert compose({}, TRANSFORMERS_FILES, BERT) == [
        "transformers", "safetensors", "bert", "fill-mask", "region:us",
    ]


def test_the_pipeline_class_of_a_diffusers_repository_follows_the_task():
    files = {"model_index.json": {"_class_name": "FluxPipeline"}}

    tags = compose({"pipeline_tag": "text-to-image", "tags": ["x"]}, ["model_index.json"], files)

    assert tags == ["diffusers", "text-to-image", "diffusers:FluxPipeline", "x", "region:us"]


def test_a_merged_task_keeps_the_authors_word_in_the_tags():
    assert compose({"pipeline_tag": "conversational"}, []) == ["conversational", "region:us"]
