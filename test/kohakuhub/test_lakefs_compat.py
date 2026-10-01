"""Which LakeFS releases KohakuHub supports (docs/deployment/lakefs.md)."""

import httpx
import pytest

from kohakuhub import lakefs_compat


@pytest.fixture(autouse=True)
def _forget_version(monkeypatch):
    monkeypatch.setattr(lakefs_compat, "_version", None)


@pytest.mark.parametrize(
    "version, status, license, reset_supported",
    [
        ("1.47.0", "unsupported", "apache-2.0", False),
        ("1.48.0", "unsupported", "apache-2.0", False),  # LakeFS's "do not use"
        ("1.48.1", "supported", "apache-2.0", True),
        ("1.70.0", "unsupported", "apache-2.0", True),  # uploads, not Reset
        ("1.70.1", "supported", "apache-2.0", True),
        ("1.86.0", "supported", "apache-2.0", True),
        ("1.87.0", "supported", "bsl-1.1", True),
        ("v1.87.0", "supported", "bsl-1.1", True),
        ("1.88.0", "supported", "bsl-1.1", True),
        ("1.89.0", "untested", "bsl-1.1", True),
        ("2.0.0-rc1", "untested", "bsl-1.1", True),
        ("dev", "unknown", None, True),
        (None, "unknown", None, True),
    ],
)
def test_each_boundary(version, status, license, reset_supported):
    result = lakefs_compat.assess(version)

    assert (result.status, result.license, result.reset_supported) == (
        status,
        license,
        reset_supported,
    )
    assert result.as_dict()["status"] == status


def test_the_messages_say_why():
    assert "Reset would leave a merge commit" in lakefs_compat.assess("1.40.0").message
    assert "without TLS" in lakefs_compat.assess("1.70.0").message
    assert '"do not use"' in lakefs_compat.assess("1.48.0").message
    assert "newest tested release, 1.88.0" in lakefs_compat.assess("1.90.0").message
    assert "Business Source License" in lakefs_compat.assess("1.87.0").message
    assert "Business Source License" not in lakefs_compat.assess("1.86.0").message


def test_the_bundled_release_is_supported_and_apache():
    bundled = ".".join(map(str, lakefs_compat.RECOMMENDED))

    assert lakefs_compat.assess(bundled).status == "supported"
    assert lakefs_compat.assess(bundled).license == "apache-2.0"


def test_remember_keeps_the_version_and_logs_once(monkeypatch):
    errors = []
    monkeypatch.setattr(lakefs_compat.logger, "error", errors.append)
    assert lakefs_compat.known().status == "unknown"

    lakefs_compat.remember("1.40.0")
    lakefs_compat.remember("1.40.0")

    assert lakefs_compat.known().version == "1.40.0"
    assert lakefs_compat.known().reset_supported is False
    assert len(errors) == 1 and "older than 1.48.1" in errors[0]


@pytest.mark.parametrize(
    "version, level",
    [("1.40.0", "ERROR"), ("1.90.0", "WARNING"), ("1.87.0", "WARNING"), ("1.86.0", "INFO")],
)
def test_remember_logs_by_severity(monkeypatch, version, level):
    logged = []
    for name in ("error", "warning", "info"):
        monkeypatch.setattr(
            lakefs_compat.logger,
            name,
            lambda message, name=name: logged.append((name.upper(), message)),
        )

    lakefs_compat.remember(version)

    assert [entry[0] for entry in logged] == [level]
    assert version in logged[0][1]


def test_an_unknown_version_is_not_kept():
    lakefs_compat.remember("1.86.0")
    lakefs_compat.remember(None)

    assert lakefs_compat.known().version == "1.86.0"


class _LakeFS:
    """Answers ``GET /api/v1/config/version`` through a mock transport."""

    def __init__(self, monkeypatch, answer):
        self.answer = answer
        self.calls = 0
        real = httpx.AsyncClient

        def handler(request):
            self.calls += 1
            assert request.url.path == "/api/v1/config/version"
            if isinstance(self.answer, Exception):
                raise self.answer
            return httpx.Response(200, json={"version": self.answer})

        monkeypatch.setattr(
            lakefs_compat.httpx,
            "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )


async def test_learn_asks_lakefs_once(monkeypatch):
    lakefs = _LakeFS(monkeypatch, "1.86.0")

    assert (await lakefs_compat.learn()).status == "supported"
    assert (await lakefs_compat.learn()).version == "1.86.0"
    assert lakefs.calls == 1


async def test_learn_tries_again_while_lakefs_is_down(monkeypatch):
    lakefs = _LakeFS(monkeypatch, httpx.ConnectError("refused"))

    assert (await lakefs_compat.learn()).status == "unknown"
    lakefs.answer = "1.40.0"
    assert (await lakefs_compat.learn()).reset_supported is False
    assert lakefs.calls == 2
