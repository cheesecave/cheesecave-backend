"""Tests for the container entrypoint's worker mode (docker/startup.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

STARTUP_PATH = Path(__file__).resolve().parents[2] / "docker" / "startup.py"


@pytest.fixture
def startup(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("khub_startup", STARTUP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CRED_FILE", tmp_path / "credentials.env")
    for name in ("KOHAKU_HUB_LAKEFS_ACCESS_KEY", "KOHAKU_HUB_LAKEFS_SECRET_KEY"):
        # setenv first so monkeypatch restores the original state even though
        # load_credentials() writes os.environ directly.
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    execs = []
    monkeypatch.setattr(module.os, "execvp", lambda file, args: execs.append(args))
    module.execs = execs
    return module


def test_run_worker_waits_for_credentials_then_execs_worker(startup, monkeypatch):
    def hub_api_writes_credentials(_seconds):
        startup.CRED_FILE.write_text(
            "KOHAKU_HUB_LAKEFS_ACCESS_KEY=ak\nKOHAKU_HUB_LAKEFS_SECRET_KEY=sk\n"
        )

    monkeypatch.setattr(startup.time, "sleep", hub_api_writes_credentials)

    startup.run_worker()

    assert startup.os.environ["KOHAKU_HUB_LAKEFS_ACCESS_KEY"] == "ak"
    assert startup.execs == [[sys.executable, "-m", "kohakuhub.worker"]]


def test_run_worker_uses_existing_credentials_env(startup, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_LAKEFS_ACCESS_KEY", "from-env")

    startup.run_worker()

    assert not startup.CRED_FILE.exists()
    assert startup.execs == [[sys.executable, "-m", "kohakuhub.worker"]]


def test_main_dispatches_worker_mode(startup, monkeypatch):
    monkeypatch.setenv("KOHAKU_HUB_LAKEFS_ACCESS_KEY", "from-env")
    monkeypatch.setattr(startup.sys, "argv", ["startup.py", "worker"])
    monkeypatch.setattr(startup, "wait_for_lakefs", lambda: pytest.fail("API bootstrap ran"))

    startup.main()

    assert startup.execs == [[sys.executable, "-m", "kohakuhub.worker"]]


def test_write_credentials_replaces_file_atomically(startup):
    startup.write_credentials("ak", "sk")

    assert startup.CRED_FILE.read_text() == (
        "KOHAKU_HUB_LAKEFS_ACCESS_KEY=ak\nKOHAKU_HUB_LAKEFS_SECRET_KEY=sk\n"
    )
    assert list(startup.CRED_FILE.parent.iterdir()) == [startup.CRED_FILE]


# --- settings are read under CHEESE_CAVE_<NAME> first, KOHAKU_HUB_<NAME> as the fallback ------------
#
# The LakeFS credentials file is a data format older images and shell scripts read, so it is still
# written with the old keys, and what it holds is exported under the keys it was written with.


@pytest.fixture
def prefixed(monkeypatch, tmp_path):
    """A loader of fresh entrypoint modules, each over a private environment without hub variables."""
    import os

    environ = {
        k: v for k, v in os.environ.items() if not k.startswith(("CHEESE_CAVE_", "KOHAKU_HUB_"))
    }
    monkeypatch.setattr(os, "environ", environ)

    def load():
        spec = importlib.util.spec_from_file_location("khub_startup_prefixed", STARTUP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.CRED_FILE = tmp_path / "creds" / "credentials.env"
        return module

    load.environ = environ
    return load


def test_the_lakefs_endpoint_defaults_and_is_read_under_either_prefix(prefixed):
    assert prefixed().LAKEFS_ENDPOINT == "http://lakefs:28000"
    prefixed.environ["KOHAKU_HUB_LAKEFS_ENDPOINT"] = "http://old:1"
    assert prefixed().LAKEFS_ENDPOINT == "http://old:1"
    prefixed.environ["CHEESE_CAVE_LAKEFS_ENDPOINT"] = "http://new:2"
    assert prefixed().LAKEFS_ENDPOINT == "http://new:2"


def test_the_worker_count_is_read_under_either_prefix(prefixed):
    module = prefixed()
    assert module.worker_count() == 4
    prefixed.environ["KOHAKU_HUB_WORKERS"] = "6"
    assert module.worker_count() == 6
    prefixed.environ["CHEESE_CAVE_WORKERS"] = "2"
    assert module.worker_count() == 2


def test_garage_is_detected_from_the_s3_endpoint_under_either_prefix(prefixed, capsys):
    module = prefixed()
    module.init_garage()
    assert capsys.readouterr().out == ""

    prefixed.environ["KOHAKU_HUB_S3_ENDPOINT"] = "http://garage:3900"
    module.init_garage()
    assert "Detected Garage" in capsys.readouterr().out

    prefixed.environ["CHEESE_CAVE_S3_ENDPOINT"] = "http://minio:9000"
    module.init_garage()
    assert capsys.readouterr().out == ""


def test_credentials_in_the_environment_under_either_prefix_are_not_replaced_by_the_file(prefixed):
    module = prefixed()
    module.write_credentials("FILE-AK", "FILE-SK")

    prefixed.environ["CHEESE_CAVE_LAKEFS_ACCESS_KEY"] = "ENV-AK"
    module.load_credentials()
    assert prefixed.environ["CHEESE_CAVE_LAKEFS_ACCESS_KEY"] == "ENV-AK"
    assert "KOHAKU_HUB_LAKEFS_ACCESS_KEY" not in prefixed.environ

    del prefixed.environ["CHEESE_CAVE_LAKEFS_ACCESS_KEY"]
    prefixed.environ["KOHAKU_HUB_LAKEFS_ACCESS_KEY"] = "OLD-ENV-AK"
    module.load_credentials()
    assert prefixed.environ["KOHAKU_HUB_LAKEFS_ACCESS_KEY"] == "OLD-ENV-AK"


def test_a_credentials_file_with_the_new_keys_is_read_as_written(prefixed):
    module = prefixed()
    module.CRED_FILE.parent.mkdir(parents=True)
    module.CRED_FILE.write_text(
        "CHEESE_CAVE_LAKEFS_ACCESS_KEY=NEW-AK\nCHEESE_CAVE_LAKEFS_SECRET_KEY=NEW-SK\n\nnot a pair\n"
    )

    module.load_credentials()

    assert prefixed.environ["CHEESE_CAVE_LAKEFS_ACCESS_KEY"] == "NEW-AK"
    assert prefixed.environ["CHEESE_CAVE_LAKEFS_SECRET_KEY"] == "NEW-SK"


def test_the_worker_needs_no_file_when_credentials_are_given_under_the_new_prefix(prefixed, monkeypatch):
    module = prefixed()
    execs = []
    monkeypatch.setattr(module.os, "execvp", lambda file, args: execs.append(args))
    prefixed.environ["CHEESE_CAVE_LAKEFS_ACCESS_KEY"] = "AK"

    module.run_worker()  # would wait for the missing file forever if it looked only at the old name

    assert execs == [[sys.executable, "-m", "kohakuhub.worker"]]
    assert not module.CRED_FILE.exists()


def test_the_server_starts_with_the_configured_worker_count_and_credentials_of_either_prefix(
    prefixed, monkeypatch
):
    module = prefixed()
    prefixed.environ.update(
        CHEESE_CAVE_WORKERS="3",
        CHEESE_CAVE_LAKEFS_ACCESS_KEY="AK",
        KOHAKU_HUB_LAKEFS_SECRET_KEY="SK",
    )
    ran = []
    monkeypatch.setattr(module, "wait_for_lakefs", lambda: None)
    monkeypatch.setattr(module, "run_migrations", lambda: None)
    monkeypatch.setattr(module, "is_initialized", lambda client: pytest.fail("set up again"))
    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(module.sys, "argv", ["startup.py"])

    module.main()

    assert ran[0][ran[0].index("--workers") + 1] == "3"


def test_first_start_sets_lakefs_up_and_keeps_writing_the_old_keys(prefixed, monkeypatch):
    module = prefixed()
    monkeypatch.setattr(module, "wait_for_lakefs", lambda: None)
    monkeypatch.setattr(module, "run_migrations", lambda: None)
    monkeypatch.setattr(module, "is_initialized", lambda client: False)
    monkeypatch.setattr(module, "do_setup", lambda client: ("NEW-AK", "NEW-SK"))
    monkeypatch.setattr(module.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(module.sys, "argv", ["startup.py"])

    module.main()

    assert prefixed.environ["KOHAKU_HUB_LAKEFS_ACCESS_KEY"] == "NEW-AK"
    assert prefixed.environ["KOHAKU_HUB_LAKEFS_SECRET_KEY"] == "NEW-SK"
    assert module.CRED_FILE.read_text().splitlines() == [
        "KOHAKU_HUB_LAKEFS_ACCESS_KEY=NEW-AK",
        "KOHAKU_HUB_LAKEFS_SECRET_KEY=NEW-SK",
    ]
