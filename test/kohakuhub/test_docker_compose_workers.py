"""Worker replicas and independent CheeseCave release images."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REPLICAS = "${KOHAKU_HUB_WORKER_REPLICAS:-1}"


@pytest.mark.parametrize("source", ["compose.yml", "docker-compose.example.yml"])
def test_worker_replicas_are_set_at_startup(source):
    compose = yaml.safe_load((ROOT / source).read_text(encoding="utf-8"))
    worker = compose["services"]["khub-worker"]
    assert "container_name" not in worker
    assert worker["deploy"]["replicas"] == REPLICAS
    assert worker["stop_grace_period"] == "45s"
    assert worker["command"] == ["python", "/app/startup.py", "worker"]
    assert worker["image"] == compose["services"]["hub-api"]["image"]
    assert worker["environment"] == compose["services"]["hub-api"]["environment"]
    assert worker["volumes"] == ["./hub-meta/hub-api:/hub-api-creds:ro"]
    assert compose["services"]["hub-web"]["image"] != worker["image"]
    assert compose["services"]["hub-admin"]["image"] != worker["image"]
    assert "volumes" not in compose["services"]["hub-web"]
    assert "volumes" not in compose["services"]["hub-admin"]


def test_source_builds_use_sibling_contexts():
    services = yaml.safe_load((ROOT / "compose.build.yml").read_text(encoding="utf-8"))["services"]
    assert services["hub-web"]["build"]["context"] == "../cheesecave-web"
    assert services["hub-admin"]["build"]["context"] == "../cheesecave-admin"
    assert services["hub-api"]["build"] == services["khub-worker"]["build"]


def test_generator_copies_the_release_template_and_refuses_overwrite(tmp_path):
    output = tmp_path / "compose.yml"
    command = [
        sys.executable,
        str(ROOT / "scripts/generate_docker_compose.py"),
        "--output",
        str(output),
    ]
    assert subprocess.run(command, capture_output=True).returncode == 0
    original = output.read_bytes()
    assert original == (ROOT / "compose.yml").read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_bytes() == original


def test_generator_creates_local_secrets_without_overwriting(tmp_path):
    output = tmp_path / ".env"
    command = [
        sys.executable,
        str(ROOT / "scripts/generate_docker_compose.py"),
        "--generate-config",
        "--output",
        str(output),
    ]
    assert subprocess.run(command, capture_output=True).returncode == 0
    generated = output.read_text(encoding="utf-8")
    assert "=CHANGE_ME" not in generated
    assert "\nCHEESECAVE_BACKEND_IMAGE=" not in generated
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_text(encoding="utf-8") == generated


GHCR = "ghcr.io/cheesecave/cheesecave-{}:${{CHEESECAVE_VERSION:-latest}}"


@pytest.mark.parametrize("source", ["compose.yml", "docker-compose.example.yml"])
def test_default_images_are_pulled_from_ghcr(source):
    services = yaml.safe_load((ROOT / source).read_text(encoding="utf-8"))["services"]
    for service, name in [("hub-api", "backend"), ("khub-worker", "backend"),
                          ("hub-web", "web"), ("hub-admin", "admin")]:
        image = services[service]["image"]
        var = f"CHEESECAVE_{name.upper()}_IMAGE"
        assert image == "${" + var + ":-" + GHCR.format(name) + "}"
        assert "build" not in services[service]


def test_source_build_overlay_tags_local_images():
    services = yaml.safe_load((ROOT / "compose.build.yml").read_text(encoding="utf-8"))["services"]
    assert services["hub-api"]["image"] == services["khub-worker"]["image"]
    for service, name in [("hub-api", "backend"), ("hub-web", "web"), ("hub-admin", "admin")]:
        assert services[service]["image"].endswith(f":-cheesecave-{name}:local}}")


def test_env_example_pulls_by_default():
    env = (ROOT / ".env.compose.example").read_text(encoding="utf-8")
    assert "\nCHEESECAVE_BACKEND_IMAGE=" not in env
    assert "# CHEESECAVE_VERSION=" in env


def test_publish_workflow_pushes_only_outside_pull_requests():
    text = (ROOT / ".github/workflows/publish-image.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["image"]
    assert job["permissions"]["packages"] == "write"
    assert workflow["permissions"] == {"contents": "read"}
    assert job["env"]["IMAGE"] == "ghcr.io/cheesecave/cheesecave-backend"
    assert "push: ${{ github.event_name != 'pull_request' }}" in text
    for step in job["steps"]:
        ref = step.get("uses", "")
        assert "@" not in ref or len(ref.split("@")[1].split()[0]) == 40


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_generated_config_precreates_user_owned_data_dirs(tmp_path):
    output = tmp_path / ".env"
    command = [sys.executable, str(ROOT / "scripts/generate_docker_compose.py"),
               "--generate-config", "--output", str(output)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    generated = output.read_text(encoding="utf-8")
    assert f"\nUID={os.getuid()}\n" in generated and f"\nGID={os.getgid()}\n" in generated
    for name in ("lakefs-data", "lakefs-cache", "valkey-data"):
        assert (tmp_path / "hub-meta" / name).stat().st_uid == os.getuid()
