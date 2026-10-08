"""Forward-looking huggingface_hub CI: failure triage decides between a new issue and a comment."""

import importlib.util
import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("hf_forward_triage", ROOT / "scripts/ci/hf_forward_triage.py")
triage_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(triage_module)

JUNIT = """<testsuite>
  <testcase classname="test.a" name="test_ok"/>
  <testcase classname="test.a" name="test_bad"><failure message="KeyError: 'sha' is missing&#10;more">trace</failure></testcase>
  <testcase classname="test.b" name="test_broken"><error>ValueError: boom</error></testcase>
</testsuite>"""


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_failing_cases_reads_failures_and_errors_only(tmp_path):
    junit = write(tmp_path / "hf.xml", JUNIT)
    assert triage_module.failing_cases(junit) == [
        ("test.a::test_bad", "KeyError: 'sha' is missing"),
        ("test.b::test_broken", "ValueError: boom"),
    ]


def test_failing_cases_without_report_is_empty(tmp_path):
    assert triage_module.failing_cases(tmp_path / "missing.xml") == []


def test_failing_case_without_message_uses_empty_first_line(tmp_path):
    junit = write(tmp_path / "hf.xml", '<testsuite><testcase classname="c" name="n"><failure/></testcase></testsuite>')
    assert triage_module.failing_cases(junit) == [("c::n", "")]


def test_signature_ignores_order_and_falls_back_to_log_tail():
    first = triage_module.signature_of([("a", "x"), ("b", "y")], "log")
    assert first == triage_module.signature_of([("b", "y"), ("a", "x")], "other log")
    assert triage_module.signature_of([], "log one\nlast line") != triage_module.signature_of([], "log two\nlast line")
    assert triage_module.signature_of([], "p" + "q" * 600) == triage_module.signature_of([], "r" + "q" * 600)
    assert len(first) == 16


def test_parse_decision_validates_the_duplicate_number():
    reply = 'Here: {"duplicate_of": 7, "title": "t", "body": "b"} done'
    assert triage_module.parse_decision(reply, {7, 9}) == {"duplicate_of": 7, "title": "t", "body": "b"}
    assert triage_module.parse_decision('{"duplicate_of": "9", "title": "t", "body": "b"}', {7, 9})["duplicate_of"] == 9
    assert triage_module.parse_decision('{"duplicate_of": 8, "title": "t", "body": "b"}', {7, 9})["duplicate_of"] is None
    assert triage_module.parse_decision('{"duplicate_of": "x", "title": "t", "body": "b"}', {7})["duplicate_of"] is None
    long = triage_module.parse_decision('{"duplicate_of": null, "title": "%s", "body": " b "}' % ("t" * 200), set())
    assert len(long["title"]) == 120 and long["body"] == "b"


def test_parse_decision_rejects_replies_without_json():
    with pytest.raises(ValueError):
        triage_module.parse_decision("no json here", set())


def test_ask_model_posts_the_messages_request_with_bearer_auth():
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        reply = {"content": [{"type": "text", "text": "hello"}, {"type": "tool_use"}, {"type": "text", "text": "!"}]}
        return Response(json.dumps(reply).encode())

    text = triage_module.ask_model("https://gateway.example/", "token-value", "prompt", opener=opener)
    assert text == "hello!"
    assert seen["url"] == "https://gateway.example/v1/messages"
    assert seen["headers"]["authorization"] == "Bearer token-value"
    assert seen["body"]["model"] == "claude-haiku-5-5"
    # Haiku may spend tokens on thinking before the JSON; a 1024 cap truncated the reply in CI.
    assert seen["body"]["max_tokens"] >= 4096
    assert seen["body"]["messages"] == [{"role": "user", "content": "prompt"}]
    assert seen["timeout"] == 120


def fake_gh(issues=None, created=None):
    """Return a runner standing in for `gh`, recording every write it receives."""
    calls = []

    def run(args, stdin=None):
        calls.append(args)
        if args[:3] == ["gh", "issue", "list"]:
            return json.dumps(issues or [])
        if args[:3] == ["gh", "issue", "create"]:
            return "https://github.example/issues/99\n"
        return ""

    run.calls = calls
    return run


CASES = [("test.a::test_bad", "KeyError: 'sha'")]
LOG = "collecting\nFAILED test.a::test_bad\n"


def args_for(tmp_path, dry_run=False):
    junit = write(tmp_path / "hf.xml", JUNIT)
    log = write(tmp_path / "hf.log", LOG)
    return triage_module.build_parser().parse_args(
        ["--junit", str(junit), "--log", str(log), "--hub-version", "2.0.0",
         "--run-url", "https://runs.example/1"] + (["--dry-run"] if dry_run else []))


def marker_issue(number, signature):
    return {"number": number, "title": "old", "body": f"text\n<!-- hf-forward-signature: {signature} -->"}


def test_same_signature_comments_without_asking_the_model(tmp_path):
    args = args_for(tmp_path)
    cases = triage_module.failing_cases(Path(args.junit))
    log_tail = Path(args.log).read_text(encoding="utf-8")[-6000:]
    signature = triage_module.signature_of(cases, log_tail)
    run = fake_gh(issues=[marker_issue(4, signature)])

    def ask(prompt):
        raise AssertionError("the model must not be consulted for an exact repeat")

    plan = triage_module.triage(args, run, ask)
    assert plan["action"] == "comment" and plan["issue"] == 4
    assert "https://runs.example/1" in plan["body"] and "2.0.0" in plan["body"]


def test_model_duplicate_comments_on_the_matching_open_issue(tmp_path):
    args = args_for(tmp_path)
    run = fake_gh(issues=[{"number": 12, "title": "same root cause", "body": "no marker"}])
    reply = json.dumps({"duplicate_of": 12, "title": "unused", "body": "unused"})
    plan = triage_module.triage(args, run, lambda prompt: reply)
    assert plan["action"] == "comment" and plan["issue"] == 12


def test_model_new_problem_creates_one_labelled_issue_with_marker(tmp_path):
    args = args_for(tmp_path)
    run = fake_gh(issues=[{"number": 12, "title": "other", "body": "x"}])
    reply = json.dumps({"duplicate_of": 55, "title": "New: range header breaks", "body": "## Details\nfix me"})
    plan = triage_module.triage(args, run, lambda prompt: reply)
    assert plan["action"] == "create"
    assert plan["title"] == "New: range header breaks"
    assert "## Details" in plan["body"]
    assert "<!-- hf-forward-signature: " in plan["body"]
    assert "https://runs.example/1" in plan["body"]


def test_model_failure_falls_back_to_a_generic_issue(tmp_path, capsys):
    args = args_for(tmp_path)
    run = fake_gh()

    def broken(prompt):
        raise RuntimeError("gateway returned 503")

    plan = triage_module.triage(args, run, broken)
    assert plan["action"] == "create"
    assert "huggingface_hub 2.0.0" in plan["title"]
    assert "test.a::test_bad" in plan["body"]
    assert "gateway returned 503" in capsys.readouterr().err


def test_no_token_uses_the_generic_issue_without_a_model(tmp_path):
    args = args_for(tmp_path)
    plan = triage_module.triage(args, fake_gh(), None)
    assert plan["action"] == "create" and "huggingface_hub 2.0.0" in plan["title"]


def test_model_chosen_empty_title_uses_the_generic_title(tmp_path):
    args = args_for(tmp_path)
    reply = json.dumps({"duplicate_of": None, "title": "  ", "body": ""})
    plan = triage_module.triage(args, fake_gh(), lambda prompt: reply)
    assert plan["title"].startswith("Forward CI: huggingface_hub 2.0.0")


def test_apply_creates_the_label_then_the_issue(tmp_path):
    run = fake_gh()
    triage_module.apply({"action": "create", "title": "T", "body": "B"}, run)
    assert run.calls[0][:3] == ["gh", "label", "create"]
    assert run.calls[1][:3] == ["gh", "issue", "create"]
    assert run.calls[1][run.calls[1].index("--title") + 1] == "T"


def test_apply_comments_on_the_existing_issue(tmp_path):
    run = fake_gh()
    triage_module.apply({"action": "comment", "issue": 3, "body": "again"}, run)
    assert run.calls == [["gh", "issue", "comment", "3", "--body", "again"]]


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, capsys):
    args = args_for(tmp_path, dry_run=True)
    run = fake_gh()
    assert triage_module.main_with(args, run, None) == 0
    assert run.calls == [["gh", "issue", "list", "--label", "hf-forward-ci", "--state", "open",
                          "--limit", "100", "--json", "number,title,body"]]
    assert json.loads(capsys.readouterr().out)["action"] == "create"


def test_main_writes_the_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    args = args_for(tmp_path, dry_run=True)
    triage_module.main_with(args, fake_gh(), None)
    assert "create" in summary.read_text(encoding="utf-8")


def test_run_gh_returns_stdout_and_reports_failures(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text("#!/bin/sh\necho \"got:$*\"\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert triage_module.run_gh(["gh", "x", "y"]) == "got:x y\n"
    fake.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        triage_module.run_gh(["gh", "anything"])


def test_main_entry_point_uses_environment_for_the_model(tmp_path, monkeypatch):
    args = ["--junit", str(tmp_path / "none.xml"), "--log", str(tmp_path / "none.log"),
            "--hub-version", "latest", "--run-url", "https://runs.example/2", "--dry-run"]
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    calls = []
    monkeypatch.setattr(triage_module, "run_gh", lambda argv, stdin=None: calls.append(argv) or "[]")
    assert triage_module.main(args) == 0
    assert calls and calls[0][:3] == ["gh", "issue", "list"]


def test_main_with_writes_when_not_a_dry_run(tmp_path):
    args = args_for(tmp_path)
    run = fake_gh()
    assert triage_module.main_with(args, run, None) == 0
    assert [call[:3] for call in run.calls[1:]] == [["gh", "label", "create"], ["gh", "issue", "create"]]
