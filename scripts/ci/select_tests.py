"""Opt-in pytest grouping for the manually dispatched backend workflow.

Existing fixture names identify tests that bootstrap the complete storage stack.
This selector never starts services; ordinary pytest invocations remain unchanged.
"""

from pathlib import Path

import pytest

SERVICE_FIXTURES = {
    "prepared_backend_test_state",
    "backend_test_state",
    "app",
    "client",
    "owner_client",
    "member_client",
    "visitor_client",
    "outsider_client",
    "admin_client",
    "live_server_url",
    "hf_api_token",
}
FAST_FILES = {
    "test_config.py",
    "test_crypto.py",
    "test_docker_startup.py",
    "test_docker_compose_workers.py",
    "test_json_safe.py",
    "test_site_homepage.py",
    "test_site_appearance.py",
    "test_repository_discovery.py",
    "test_repository_visibility.py",
    "test_social.py",
    "test_user_repo_activity.py",
    "test_trending.py",
}


def pytest_addoption(parser):
    parser.addoption(
        "--ci-category",
        choices=("fast", "migrations", "services", "hf"),
        help="Select a manual CI regression category; omitted means the normal full suite",
    )


def pytest_ignore_collect(collection_path, config):
    # Unrelated route modules call init_db at import time. Keep schema upgrades
    # independent of that fresh-schema initialization, before collecting tests.
    if config.getoption("--ci-category") == "migrations":
        path = Path(str(collection_path))
        if path.is_file() and path.name.startswith("test_") and path.suffix == ".py":
            return not path.name.endswith("_migrations.py")
    return None


def pytest_collection_modifyitems(config, items):
    category = config.getoption("--ci-category")
    if category is None or category == "services":
        return
    selected, deselected = [], []
    for item in items:
        path = Path(str(item.path))
        service_backed = bool(SERVICE_FIXTURES.intersection(item.fixturenames))
        integration = item.get_closest_marker("integration") is not None
        if category == "fast":
            keep = (
                (path.name.endswith("_unit.py") or path.name in FAST_FILES)
                and not service_backed
                and not integration
            )
        elif category == "migrations":
            # Legacy migration tests that need the full seeded stack run in services.
            keep = path.name.endswith("_migrations.py") and not service_backed
        else:
            keep = (
                path.name.startswith("test_huggingface_hub_")
                or path.name == "test_real_hf_hub_end_to_end.py"
            )
        (selected if keep else deselected).append(item)
    if not selected:
        raise pytest.UsageError(f"No tests selected for CI category {category!r}")
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"[ci] {category}: {len(selected)} selected, {len(deselected)} deselected"
        )
