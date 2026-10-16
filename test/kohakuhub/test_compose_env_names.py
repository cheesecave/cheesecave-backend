"""The Compose files hand the application CHEESE_CAVE_* names and still honour an old .env.

A deployment written before the rename has KOHAKU_HUB_* keys in its ``.env``. The shipped
Compose files interpolate those keys, so the interpolation must accept the new key first and
the old one as the fallback; a container variable must carry the new name (the application
reads it first). Static checks cover the shape; ``docker compose config`` renders the real
thing when Compose is installed (it needs no running daemon).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ["compose.yml", "docker-compose.example.yml"]
OLD_INTERPOLATION = re.compile(r"\$\{KOHAKU_HUB_([A-Z0-9_]+)")
NESTED_OLD = re.compile(r"\$\{CHEESE_CAVE_([A-Z0-9_]+):-\$\{KOHAKU_HUB_\1[:}]")


def _environment_names(compose_file: str) -> list[str]:
    document = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    names = []
    for service in document["services"].values():
        environment = service.get("environment") or []
        if isinstance(environment, dict):
            names.extend(environment)
        else:
            names.extend(entry.split("=", 1)[0] for entry in environment)
    return names


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_no_container_variable_carries_the_old_prefix(compose_file):
    assert [n for n in _environment_names(compose_file) if n.startswith("KOHAKU_HUB_")] == []


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_the_application_receives_its_settings_under_the_new_prefix(compose_file):
    names = _environment_names(compose_file)
    assert "CHEESE_CAVE_BASE_URL" in names
    assert "CHEESE_CAVE_SESSION_SECRET" in names
    assert "CHEESE_CAVE_DATABASE_URL" in names


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_an_old_env_key_is_only_ever_the_fallback_of_its_new_key(compose_file):
    text = (ROOT / compose_file).read_text(encoding="utf-8")
    for match in OLD_INTERPOLATION.finditer(text):
        context = text[max(0, match.start() - 40) : match.end() + 2]
        assert f"${{CHEESE_CAVE_{match.group(1)}:-${{KOHAKU_HUB_{match.group(1)}" in context, context
    assert NESTED_OLD.search(text), "no interpolation takes an old key at all"


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_the_worker_replica_count_accepts_either_key(compose_file):
    if compose_file != "compose.yml":
        pytest.skip("only the full compose file runs a worker service")
    text = (ROOT / compose_file).read_text(encoding="utf-8")
    assert "${CHEESE_CAVE_WORKER_REPLICAS:-${KOHAKU_HUB_WORKER_REPLICAS:-1}}" in text


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "compose", "version"], capture_output=True)
    return result.returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="docker compose is not installed")

REQUIRED = {
    "SESSION_SECRET": "s",
    "ADMIN_SECRET_TOKEN": "a",
    "DATABASE_KEY": "k",
}
OTHER_REQUIRED = "LAKEFS_AUTH_ENCRYPT_SECRET_KEY=lakefs-secret\n"


def _render(tmp_path, env_text: str, compose_file: str = "compose.yml"):
    env_file = tmp_path / "test.env"
    env_file.write_text(env_text + OTHER_REQUIRED)
    result = subprocess.run(
        [
            "docker", "compose", "--env-file", str(env_file),
            "-f", str(ROOT / compose_file), "--project-directory", str(ROOT),
            "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
    )
    return result


def _container_env(rendered_stdout: str, service: str = "hub-api") -> dict[str, str]:
    return json.loads(rendered_stdout)["services"][service]["environment"]


@needs_compose
@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_an_env_file_with_the_old_keys_still_renders_and_reaches_the_container(tmp_path, compose_file):
    old = "".join(f"KOHAKU_HUB_{k}={v}\n" for k, v in REQUIRED.items())
    old += "KOHAKU_HUB_SITE_NAME=Old Name\nKOHAKU_HUB_BASE_URL=https://old.example\n"

    result = _render(tmp_path, old, compose_file)

    assert result.returncode == 0, result.stderr
    env = _container_env(result.stdout)
    assert env["CHEESE_CAVE_SESSION_SECRET"] == "s"
    assert env["CHEESE_CAVE_BASE_URL"] == "https://old.example"
    assert env["CHEESE_CAVE_SITE_NAME"] == "Old Name" or compose_file != "compose.yml"
    assert not [k for k in env if k.startswith("KOHAKU_HUB_")]


@needs_compose
@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_an_env_file_with_the_new_keys_renders(tmp_path, compose_file):
    new = "".join(f"CHEESE_CAVE_{k}={v}\n" for k, v in REQUIRED.items())
    new += "CHEESE_CAVE_BASE_URL=https://new.example\n"

    result = _render(tmp_path, new, compose_file)

    assert result.returncode == 0, result.stderr
    env = _container_env(result.stdout)
    assert env["CHEESE_CAVE_SESSION_SECRET"] == "s"
    assert env["CHEESE_CAVE_BASE_URL"] == "https://new.example"


@needs_compose
def test_a_new_key_wins_over_an_old_one_in_the_same_env_file(tmp_path):
    both = "".join(f"CHEESE_CAVE_{k}={v}\n" for k, v in REQUIRED.items())
    both += "KOHAKU_HUB_SESSION_SECRET=old-secret\n"
    both += "KOHAKU_HUB_BASE_URL=https://old.example\nCHEESE_CAVE_BASE_URL=https://new.example\n"

    result = _render(tmp_path, both)

    assert result.returncode == 0, result.stderr
    env = _container_env(result.stdout)
    assert env["CHEESE_CAVE_SESSION_SECRET"] == "s"
    assert env["CHEESE_CAVE_BASE_URL"] == "https://new.example"


@needs_compose
def test_a_missing_secret_names_both_keys_in_the_error(tmp_path):
    result = _render(tmp_path, "")

    assert result.returncode != 0
    assert "CHEESE_CAVE_SESSION_SECRET" in result.stderr
    assert "KOHAKU_HUB_SESSION_SECRET" in result.stderr


@needs_compose
def test_the_worker_gets_the_same_settings_as_the_api(tmp_path):
    old = "".join(f"KOHAKU_HUB_{k}={v}\n" for k, v in REQUIRED.items())

    result = _render(tmp_path, old)

    assert result.returncode == 0, result.stderr
    assert _container_env(result.stdout, "khub-worker")["CHEESE_CAVE_SESSION_SECRET"] == "s"
