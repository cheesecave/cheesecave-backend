"""Worker replicas and independent CheeseCave release images."""

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
    assert "CHEESECAVE_BACKEND_IMAGE=cheesecave-backend:local" in generated
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_text(encoding="utf-8") == generated
