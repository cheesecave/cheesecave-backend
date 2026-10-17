"""The operator scripts read and write environment names under the current prefix.

``CHEESE_CAVE_<NAME>`` is what they produce and prefer; ``KOHAKU_HUB_<NAME>`` in an existing
file or environment is still understood (the credentials file, which older images and shell
scripts read, keeps the old keys).
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- migrate_config: an existing docker-compose.yml, old or new keys -------------------------

LEGACY_COMPOSE = """services:
  hub-api:
    environment:
      - KOHAKU_HUB_SESSION_SECRET=old-secret
      - KOHAKU_HUB_SITE_NAME=Old Site # a comment
      - KOHAKU_HUB_BASE_URL=https://old.example
  khub-worker:
    environment:
      - KOHAKU_HUB_SITE_NAME=not the api
"""


def test_an_old_compose_file_is_read_under_the_new_keys(tmp_path):
    migrate = _load("migrate_config")
    path = tmp_path / "docker-compose.yml"
    path.write_text(LEGACY_COMPOSE)

    env = migrate.read_existing_docker_compose(path)

    assert env == {
        "CHEESE_CAVE_SESSION_SECRET": "old-secret",
        "CHEESE_CAVE_SITE_NAME": "Old Site",
        "CHEESE_CAVE_BASE_URL": "https://old.example",
    }


def test_a_compose_file_with_both_prefixes_keeps_the_new_one(tmp_path):
    migrate = _load("migrate_config")
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        """services:
  hub-api:
    environment:
      - KOHAKU_HUB_SITE_NAME=old
      - CHEESE_CAVE_SITE_NAME=new
      - KOHAKU_HUB_SITE_NAME=old again
"""
    )

    assert migrate.read_existing_docker_compose(path) == {"CHEESE_CAVE_SITE_NAME": "new"}


def test_a_new_style_compose_file_is_read_too(tmp_path):
    migrate = _load("migrate_config")
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "services:\n  hub-api:\n    environment:\n      - CHEESE_CAVE_SITE_NAME=new\n"
    )

    assert migrate.read_existing_docker_compose(path) == {"CHEESE_CAVE_SITE_NAME": "new"}


def test_the_migrated_values_are_written_into_the_new_template(tmp_path):
    migrate = _load("migrate_config")
    out = tmp_path / "docker-compose.yml"

    migrate.write_docker_compose(
        out,
        {"CHEESE_CAVE_SITE_NAME": "Old Site"},
        base_content="    environment:\n      - CHEESE_CAVE_SITE_NAME=${CHEESE_CAVE_SITE_NAME:-x} # name\n      - CHEESE_CAVE_OTHER=keep\n",
    )

    written = out.read_text()
    assert "- CHEESE_CAVE_SITE_NAME=Old Site # name" in written
    assert "- CHEESE_CAVE_OTHER=keep" in written


def test_an_existing_old_file_is_updated_in_place_and_renamed(tmp_path):
    # main() reads the existing docker-compose.yml as the base it writes back into, so the lines it
    # replaces are the old-prefixed ones the operator has.
    migrate = _load("migrate_config")
    out = tmp_path / "docker-compose.yml"

    migrate.write_docker_compose(
        out,
        {"CHEESE_CAVE_SITE_NAME": "Old Site", "CHEESE_CAVE_SESSION_SECRET": "keep-me"},
        base_content=LEGACY_COMPOSE,
    )

    written = out.read_text()
    assert "- CHEESE_CAVE_SITE_NAME=Old Site" in written
    assert "- CHEESE_CAVE_SESSION_SECRET=keep-me" in written
    assert "KOHAKU_HUB_SITE_NAME" not in written and "KOHAKU_HUB_SESSION_SECRET" not in written
    # a line with no migrated value is left exactly as it was
    assert "- KOHAKU_HUB_BASE_URL=https://old.example" in written


def test_new_fields_are_offered_under_the_new_prefix():
    migrate = _load("migrate_config")

    migrated = migrate.migrate_docker_compose({}, auto_mode=True)

    assert "CHEESE_CAVE_DATABASE_KEY" in migrated
    assert "CHEESE_CAVE_FALLBACK_REQUIRE_AUTH" in migrated
    assert not [key for key in migrated if key.startswith("KOHAKU_HUB_")]


# --- generate_secret: names it advises ----------------------------------------------------------


def test_the_secret_generator_names_the_new_variables(capsys):
    generate = _load("generate_secret")

    generate.generate_multiple_secrets()

    out = capsys.readouterr().out
    assert "CHEESE_CAVE_SESSION_SECRET" in out
    assert "CHEESE_CAVE_ADMIN_SECRET_TOKEN" in out
    assert "KOHAKU_HUB_" not in out


# --- sync_lakefs_credentials: the credentials file, old keys or new -----------------------------


def test_lakefs_credentials_are_read_under_either_key():
    sync = _load("sync_lakefs_credentials")

    old = {"KOHAKU_HUB_LAKEFS_ACCESS_KEY": "old-ak", "KOHAKU_HUB_LAKEFS_SECRET_KEY": "old-sk"}
    new = {"CHEESE_CAVE_LAKEFS_ACCESS_KEY": "new-ak"}
    both = {**old, **new}

    assert sync.lakefs_credential(old, "ACCESS_KEY") == "old-ak"
    assert sync.lakefs_credential(new, "ACCESS_KEY") == "new-ak"
    assert sync.lakefs_credential(both, "ACCESS_KEY") == "new-ak"
    assert sync.lakefs_credential(both, "SECRET_KEY") == "old-sk"
    assert sync.lakefs_credential({}, "ACCESS_KEY") is None


# --- scripts/dev/init_lakefs.py -----------------------------------------------------------------


def _load_dev(name: str):
    spec = importlib.util.spec_from_file_location(f"dev_{name}", ROOT / "scripts" / "dev" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dev_lakefs_endpoint_default_follows_either_prefix(monkeypatch):
    for name in ("CHEESE_CAVE_LAKEFS_ENDPOINT", "KOHAKU_HUB_LAKEFS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KOHAKU_HUB_LAKEFS_ENDPOINT", "http://old:1")
    init = _load_dev("init_lakefs")
    assert init.env_or("LAKEFS_ENDPOINT", "dflt") == "http://old:1"


def test_the_dev_credentials_file_keeps_the_old_keys_and_reads_either(tmp_path):
    init = _load_dev("init_lakefs")
    path = tmp_path / "credentials.env"

    init.write_credentials(path, "AK", "SK")

    assert path.read_text().splitlines()[:2] == [
        "KOHAKU_HUB_LAKEFS_ACCESS_KEY=AK",
        "KOHAKU_HUB_LAKEFS_SECRET_KEY=SK",
    ]
    assert init.credential(init.read_credentials(path), "ACCESS_KEY") == "AK"
    assert init.credential({"CHEESE_CAVE_LAKEFS_SECRET_KEY": "NEW"}, "SECRET_KEY") == "NEW"
    assert init.credential({}, "SECRET_KEY") is None


# --- migration 008: the auto-confirm switch ----------------------------------------------------


def _load_migration_008():
    path = ROOT / "scripts" / "db_migrations" / "008_foreignkey_refactoring.py"
    spec = importlib.util.spec_from_file_location("migration_008_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_008_auto_confirms_under_either_prefix(monkeypatch):
    for name in ("CHEESE_CAVE_AUTO_MIGRATE", "KOHAKU_HUB_AUTO_MIGRATE"):
        monkeypatch.delenv(name, raising=False)
    migration = _load_migration_008()
    assert migration.auto_confirm_enabled() is False

    monkeypatch.setenv("KOHAKU_HUB_AUTO_MIGRATE", "true")
    assert migration.auto_confirm_enabled() is True

    monkeypatch.setenv("CHEESE_CAVE_AUTO_MIGRATE", "no")
    assert migration.auto_confirm_enabled() is False  # the new prefix wins

    monkeypatch.setenv("CHEESE_CAVE_AUTO_MIGRATE", "YES")
    assert migration.auto_confirm_enabled() is True
