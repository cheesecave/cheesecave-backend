"""The corpus itself and the guard: no library and no server needed, so these run everywhere."""

from __future__ import annotations

import socket
import zipfile
from dataclasses import replace

import pytest

from test.kohakuhub.support import hub_corpus
from test.kohakuhub.support.downstream import only_this_host  # noqa: F401

WEIGHTS = {".bin", ".pt", ".pth", ".ckpt", ".h5", ".msgpack", ".onnx", ".gguf", ".tflite", ".npz", ".npy", ".pkl", ".ot", ".pb"}
MODELS = hub_corpus.repos("model")
DATASETS = hub_corpus.repos("dataset")


def test_the_guard_refuses_a_connection_that_leaves_the_machine():
    with pytest.raises(AssertionError, match="left the machine"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


@pytest.mark.parametrize("repo", MODELS + DATASETS, ids=hub_corpus.param_id)
def test_every_file_of_the_corpus_is_what_was_sampled(repo):
    assert hub_corpus.verify(repo) == []
    assert repo.revision and len(repo.revision) == 40


def test_no_weights_are_kept_and_a_safetensors_file_only_as_its_header():
    for repo in MODELS + DATASETS:
        for entry in repo.files:
            path = entry["path"].lower()
            assert not any(path.endswith(suffix) for suffix in WEIGHTS), entry["path"]
            if path.endswith(".safetensors"):
                assert entry["header_only"] and entry["size"] <= entry["full_size"]


def test_verify_notices_a_missing_and_a_changed_file(tmp_path, monkeypatch):
    repo = next(r for r in MODELS if len(r.files) > 1)
    first, second = repo.files[0]["path"], repo.files[1]["path"]
    altered = tmp_path / "repos" / repo.type / f"{repo.alias}.zip"
    altered.parent.mkdir(parents=True)
    with zipfile.ZipFile(repo.archive) as source, zipfile.ZipFile(altered, "w") as target:
        for name in source.namelist():
            if name != first:  # one file gone
                target.writestr(name, b"changed" if name == second else source.read(name))
    monkeypatch.setattr(hub_corpus, "CORPUS_DIR", tmp_path)
    assert sorted(hub_corpus.verify(replace(repo))) == sorted([f"missing {first}", f"changed {second}"])


def test_a_repository_is_released_once_and_its_archive_holds_exactly_its_files():
    repo = MODELS[0]
    with zipfile.ZipFile(repo.archive) as zipped:
        assert sorted(zipped.namelist()) == repo.paths() == sorted(repo.paths())
    assert repo.directory == repo.directory  # the same temporary directory every time
    assert (repo.directory / repo.files[0]["path"]).is_file()



def _distinct(repos, name):
    """How many different files called ``name`` the repos have (by content): cases that differ."""
    return len({f["sha256"] for r in repos for f in r.files if f["path"].rsplit("/", 1)[-1] == name})


def test_each_library_has_at_least_forty_different_real_cases():
    names = lambda r: {p.rsplit("/", 1)[-1] for p in r.paths()}  # noqa: E731
    tokenizer_files = {"tokenizer.json", "tokenizer_config.json", "vocab.txt", "vocab.json", "spiece.model", "tokenizer.model"}
    with_tokenizer = [r for r in MODELS if names(r) & tokenizer_files]
    with_tokenizer_json = [r for r in MODELS if "tokenizer.json" in names(r)]
    with_weights = [r for r in MODELS if any(f.get("header_only") for f in r.files)]
    assert len(with_tokenizer) >= 40 and len(with_weights) >= 40 and len(DATASETS) >= 40
    assert len(with_tokenizer_json) >= 40
    assert _distinct(MODELS, "config.json") >= 40  # transformers configs
    assert _distinct(with_tokenizer_json, "tokenizer.json") >= 40  # tokenizers
    assert len({f["sha256"] for r in with_weights for f in r.files if f["path"].endswith(".safetensors")}) >= 40
    assert len({r.hub.get("model_type") for r in MODELS if r.hub.get("model_type")}) >= 40  # architectures differ
    layouts = {tuple(sorted({p.rsplit(".", 1)[-1] for p in r.paths()})) for r in DATASETS}
    assert len(layouts) >= 8  # formats and layouts of the datasets differ


JUNIT = """<testsuites><testsuite>
<testcase classname="test.kohakuhub.api.test_downstream_demo" name="a"/>
<testcase classname="test.kohakuhub.api.test_downstream_demo" name="b"/>
<testcase classname="test.kohakuhub.api.test_downstream_demo" name="c"><skipped message="the library cannot read the sampled files: X"/></testcase>
<testcase classname="test.kohakuhub.api.test_downstream_demo" name="d"><failure message="boom"/></testcase>
<testcase classname="test.kohakuhub.api.test_downstream_support" name="corpus"/>
{extra}
</testsuite></testsuites>"""


def _junit(tmp_path, extra=""):
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT.format(extra=extra))
    return str(path)


def test_a_cell_passes_when_enough_cases_passed_and_skips_say_the_version_cannot_read_the_files(tmp_path, capsys):
    from scripts.ci import check_downstream

    assert check_downstream.main([_junit(tmp_path), "--lib", "demo", "--min-passed", "2"]) == 0
    assert "passed 2, skipped because the version cannot read the files 1" in capsys.readouterr().out


def test_a_cell_fails_for_too_few_passes_and_for_a_skip_that_is_not_the_versions(tmp_path, capsys):
    from scripts.ci import check_downstream

    missing = '<testcase classname="test.kohakuhub.api.test_downstream_demo" name="e"><skipped message="could not import \'datasets\'"/></testcase>'
    assert check_downstream.main([_junit(tmp_path), "--lib", "demo", "--min-passed", "3"]) == 1
    assert check_downstream.main([_junit(tmp_path, missing), "--lib", "demo", "--min-passed", "1"]) == 1
    err = capsys.readouterr().err
    assert "only 2 cases passed, at least 3 are needed" in err and "skipped for another reason: e" in err
