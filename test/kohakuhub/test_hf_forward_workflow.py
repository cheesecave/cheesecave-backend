"""Static checks for the daily forward-looking huggingface_hub workflow."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/hf-forward.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_runs_daily_at_an_off_minute_and_manually(workflow):
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert triggers["schedule"][0]["cron"].split()[0] not in ("0", "30")
    assert "dry_run" in triggers["workflow_dispatch"]["inputs"]
    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_only_the_hf_client_marked_tests_run(workflow):
    steps = workflow["jobs"]["latest-hf-client"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "-m hf_client" in commands
    assert "--ci-category" not in commands


def test_latest_client_is_installed_and_recorded(workflow):
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["latest-hf-client"]["steps"])
    assert "pip install --upgrade huggingface_hub" in commands
    assert "HUB_VERSION=" in commands


def test_triage_runs_only_after_a_failure_and_reads_the_secrets(workflow):
    steps = workflow["jobs"]["latest-hf-client"]["steps"]
    triage = next(step for step in steps if "hf_forward_triage.py" in step.get("run", ""))
    assert triage["if"] == "${{ failure() }}"
    assert triage["env"]["ANTHROPIC_BASE_URL"] == "${{ secrets.ANTHROPIC_BASE_URL }}"
    assert triage["env"]["ANTHROPIC_AUTH_TOKEN"] == "${{ secrets.ANTHROPIC_AUTH_TOKEN }}"
    assert "--junit lint-reports/ci/hf-forward.xml" in triage["run"]
    assert "lint-reports/ci/hf-forward-triage.json" in triage["run"]


def test_permissions_are_minimal(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["latest-hf-client"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}


def test_every_action_is_pinned_to_a_commit(workflow):
    for step in workflow["jobs"]["latest-hf-client"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_no_pull_request_or_push_trigger_exists():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request" not in text and "push:" not in text
