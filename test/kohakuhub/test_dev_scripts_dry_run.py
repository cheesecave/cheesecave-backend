"""scripts/dev/up_infra.sh and run_backend.sh, run end to end with stubs, take either prefix.

Both scripts start containers and a backend, so they run here against a copy of ``scripts/dev``
in a scratch tree, with a ``docker`` that only records its arguments and a Python that only prints
what it was given. What is checked is what the scripts hand on: the settings LakeFS is started
with (``up_infra.sh``) and the variables the backend is started with (``run_backend.sh``).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
# nothing exists yet, so every ensure_* creates its container; `network inspect` fails, `info` is silent
case "$1" in
  ps) exit 0;;
  network) [ "$2" = inspect ] && exit 1; exit 0;;
  info) exit 0;;
esac
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    scripts = tmp_path / "scripts" / "dev"
    scripts.mkdir(parents=True)
    for name in ("up_infra.sh", "run_backend.sh", "envnames.sh"):
        shutil.copy(ROOT / "scripts" / "dev" / name, scripts / name)
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return tmp_path


def _run(tree, script, env_file_text, extra_env=None, args=()):
    (tree / ".env.dev").write_text(env_file_text)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CHEESE_CAVE_", "KOHAKU_HUB_", "DEV_", "VIRTUAL_ENV"))
    }
    env.update(
        PATH=f"{tree}/bin:{env['PATH']}",
        DOCKER_LOG=str(tree / "docker.log"),
        PYTHON_BIN=str(tree / "bin" / "fakepython"),
        PYTHON_LOG=str(tree / "python.log"),
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(tree / "scripts" / "dev" / script), *args],
        env=env, capture_output=True, text=True, timeout=60, cwd=tree,
    )


def _lakefs_run_line(tree):
    lines = (tree / "docker.log").read_text().splitlines()
    return next(line for line in lines if "LAKEFS_BLOCKSTORE_S3_BUCKET" in line)


@pytest.mark.parametrize("prefix", ["KOHAKU_HUB_", "CHEESE_CAVE_"])
def test_up_infra_hands_lakefs_the_s3_settings_under_either_prefix(tree, prefix):
    env_file = (
        f"{prefix}S3_BUCKET=bucket-from-{prefix.lower()}env\n"
        f"{prefix}S3_ACCESS_KEY=ak-{prefix.lower()}\n"
        f"{prefix}S3_SECRET_KEY=sk-{prefix.lower()}\n"
        f"{prefix}S3_REGION=eu-test-1\n"
    )

    result = _run(tree, "up_infra.sh", env_file)

    assert result.returncode == 0, result.stderr
    line = _lakefs_run_line(tree)
    assert f"LAKEFS_BLOCKSTORE_S3_BUCKET=bucket-from-{prefix.lower()}env" in line
    assert f"LAKEFS_BLOCKSTORE_S3_CREDENTIALS_ACCESS_KEY_ID=ak-{prefix.lower()}" in line
    assert f"LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SECRET_ACCESS_KEY=sk-{prefix.lower()}" in line
    assert "LAKEFS_BLOCKSTORE_S3_REGION=eu-test-1" in line


def test_up_infra_defaults_follow_the_minio_dev_credentials(tree):
    result = _run(tree, "up_infra.sh", "DEV_MINIO_ROOT_USER=dev-root\nDEV_MINIO_ROOT_PASSWORD=dev-pw\n")

    assert result.returncode == 0, result.stderr
    line = _lakefs_run_line(tree)
    assert "LAKEFS_BLOCKSTORE_S3_BUCKET=hub-storage" in line
    assert "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_ACCESS_KEY_ID=dev-root" in line
    assert "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SECRET_ACCESS_KEY=dev-pw" in line
    assert "LAKEFS_BLOCKSTORE_S3_REGION=us-east-1" in line


def test_up_infra_prefers_the_new_prefix_when_the_env_file_has_both(tree):
    env_file = "KOHAKU_HUB_S3_BUCKET=old-bucket\nCHEESE_CAVE_S3_BUCKET=new-bucket\n"

    assert _run(tree, "up_infra.sh", env_file).returncode == 0

    assert "LAKEFS_BLOCKSTORE_S3_BUCKET=new-bucket" in _lakefs_run_line(tree)


FAKE_PYTHON = """#!/bin/sh
{
  echo "ARGS $*"
  echo "CACHE_URL=${CHEESE_CAVE_CACHE_URL-<unset>}"
  echo "AUTO_SEED=${CHEESE_CAVE_DEV_AUTO_SEED-<unset>}"
} >> "$PYTHON_LOG"
exit 0
"""


@pytest.fixture
def backend_tree(tree):
    python = tree / "bin" / "fakepython"
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    (tree / "hub-meta" / "dev" / "lakefs").mkdir(parents=True)
    return tree


def _python_log(tree):
    return (tree / "python.log").read_text()


def test_run_backend_defaults_the_cache_url_and_seeds(backend_tree):
    result = _run(backend_tree, "run_backend.sh", "", args=("--prepare-only",))

    assert result.returncode == 0, result.stderr
    log = _python_log(backend_tree)
    assert "CACHE_URL=redis://127.0.0.1:26379/0" in log
    assert "seed_demo_data.py" in log


@pytest.mark.parametrize("prefix", ["KOHAKU_HUB_", "CHEESE_CAVE_"])
def test_run_backend_keeps_a_cache_url_and_the_seed_switch_set_under_either_prefix(backend_tree, prefix):
    env_file = f"{prefix}CACHE_URL=redis://cache.example:6380/1\n{prefix}DEV_AUTO_SEED=false\n"

    result = _run(backend_tree, "run_backend.sh", env_file, args=("--prepare-only",))

    assert result.returncode == 0, result.stderr
    log = _python_log(backend_tree)
    assert "CACHE_URL=redis://cache.example:6380/1" in log
    assert "seed_demo_data.py" not in log
