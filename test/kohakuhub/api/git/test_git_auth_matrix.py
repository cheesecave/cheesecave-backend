"""What git and git-lfs are told, by caller, repository and operation, through the real application.

Real clients only authenticate after a ``401`` with a challenge, so the answer to
the first, credential-less request decides whether anything works at all. The
rules, as the Hugging Face Hub gives them:

* an unauthenticated read of a private or missing repository is ``401`` plus a
  ``WWW-Authenticate`` challenge, identical for both, so it never says which;
* a push needs a valid user before the repository is even looked up;
* a signed-in caller who cannot see a repository gets ``404`` for private and
  missing alike (never ``403``, which would say "it exists"); ``403`` only where
  the repository is readable and not writable;
* an invalid credential is treated as anonymous;
* the same holds for the LFS batch route, with its own content type and
  ``LFS-Authenticate`` challenges.

Roles come from the shared baseline: ``owner`` (public model ``owner/demo-model``
and organization ``acme-labs``), ``member`` (admin of ``acme-labs``), ``visitor``
(read-only in ``acme-labs``), ``outsider`` (no access). ``acme-labs/private-dataset``
is private.
"""

from __future__ import annotations

import base64
import itertools
import json

import httpx
import pytest
import pytest_asyncio

from test.kohakuhub.support.bootstrap import DEFAULT_PASSWORD

UNAUTH = ("anonymous", "invalid Basic", "invalid Bearer")
SIGNED_IN = ("outsider", "visitor", "member", "owner Basic", "owner Bearer")
CALLERS = UNAUTH + SIGNED_IN
OWNERS = ("owner Basic", "owner Bearer")

LFS_HEADERS = {
    "Accept": "application/vnd.git-lfs+json",
    "Content-Type": "application/vnd.git-lfs+json",
}
OID = "c" * 64


class Repo:
    def __init__(self, base, readers, writers, missing=False):
        self.base = base
        self.readers = set(readers)
        self.writers = set(writers)
        self.missing = missing


EVERYONE = set(CALLERS)
REPOS = {
    "public model": Repo("/owner/demo-model.git", EVERYONE, OWNERS),
    "public dataset": Repo("/datasets/owner/e2e-public-dataset.git", EVERYONE, OWNERS),
    "public space": Repo("/spaces/owner/e2e-public-space.git", EVERYONE, OWNERS),
    "private model": Repo("/owner/e2e-private-model.git", OWNERS, OWNERS),
    "private dataset": Repo(
        "/datasets/acme-labs/private-dataset.git",
        {"visitor", "member", *OWNERS},
        {"member", *OWNERS},
    ),
    "missing model": Repo("/owner/e2e-missing.git", (), (), missing=True),
    "missing dataset": Repo("/datasets/owner/e2e-missing.git", (), (), missing=True),
    "missing space": Repo("/spaces/owner/e2e-missing.git", (), (), missing=True),
}


def _basic(username, token):
    return "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()




async def _ensure_repo(client, repo_type, name, private):
    response = await client.post(
        "/api/repos/create", json={"type": repo_type, "name": name, "private": private}
    )
    assert response.status_code in (200, 409), response.text


_PREPARED = {}


async def _prepare(app):
    """Tokens and the extra repositories, made once for the module (every case reuses them)."""
    if _PREPARED:
        return _PREPARED["headers"]
    tokens = {}
    for role in ("outsider", "visitor", "member", "owner"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", follow_redirects=False
        ) as client:
            login = await client.post(
                "/api/auth/login", json={"username": role, "password": DEFAULT_PASSWORD}
            )
            login.raise_for_status()
            created = await client.post("/api/auth/tokens/create", json={"name": "git-matrix"})
            created.raise_for_status()
            tokens[role] = created.json()["token"]
            if role == "owner":
                await _ensure_repo(client, "dataset", "e2e-public-dataset", False)
                await _ensure_repo(client, "space", "e2e-public-space", False)
                await _ensure_repo(client, "model", "e2e-private-model", True)
    _PREPARED["headers"] = {
        "anonymous": {},
        "invalid Basic": {"Authorization": _basic("x", "hf_notatoken")},
        "invalid Bearer": {"Authorization": "Bearer hf_notatoken"},
        "outsider": {"Authorization": _basic("outsider", tokens["outsider"])},
        "visitor": {"Authorization": _basic("visitor", tokens["visitor"])},
        "member": {"Authorization": _basic("member", tokens["member"])},
        "owner Basic": {"Authorization": _basic("owner", tokens["owner"])},
        "owner Bearer": {"Authorization": f"Bearer {tokens['owner']}"},
    }
    return _PREPARED["headers"]


@pytest_asyncio.fixture
async def callers(app, backend_test_state):
    headers = await _prepare(app)
    # a clean client: only the header authenticates, never a leftover session cookie
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as anon:

        async def call(caller, method, path, **kwargs):
            return await anon.request(
                method, path, headers={**headers[caller], **kwargs.pop("headers", {})}, **kwargs
            )

        yield call


def expected_read(repo, caller):
    if caller in repo.readers:
        return 200
    return 401 if caller in UNAUTH else 404


def expected_write(repo, caller, ok):
    if caller in UNAUTH:
        return 401
    if repo.missing or caller not in repo.readers:
        return 404
    return ok if caller in repo.writers else 403


MATRIX = list(itertools.product(REPOS, CALLERS))


def _ids(items):
    return [f"{repo}|{caller}" for repo, caller in items]


# --- git: reads -------------------------------------------------------------


@pytest.mark.parametrize("repo_name,caller", MATRIX, ids=_ids(MATRIX))
async def test_reads_follow_the_hub_rules(callers, repo_name, caller):
    repo = REPOS[repo_name]

    advert = await callers(caller, "GET", f"{repo.base}/info/refs?service=git-upload-pack")
    post = await callers(
        caller,
        "POST",
        f"{repo.base}/git-upload-pack",
        content=b"0000",
        headers={"Content-Type": "application/x-git-upload-pack-request"},
    )

    want = expected_read(repo, caller)
    assert advert.status_code == want, advert.text
    assert post.status_code == want, post.text
    if want == 401:
        assert advert.headers["www-authenticate"] == 'Basic realm="git-frontend"'
        assert advert.headers["content-type"].startswith("text/plain")
        assert advert.text == "Invalid username or password."


# --- git: writes ------------------------------------------------------------


@pytest.mark.parametrize("repo_name,caller", MATRIX, ids=_ids(MATRIX))
async def test_writes_follow_the_hub_rules(callers, repo_name, caller):
    repo = REPOS[repo_name]

    advert = await callers(caller, "GET", f"{repo.base}/info/refs?service=git-receive-pack")
    post = await callers(
        caller,
        "POST",
        f"{repo.base}/git-receive-pack",
        content=b"0000",
        headers={"Content-Type": "application/x-git-receive-pack-request"},
    )

    assert advert.status_code == expected_write(repo, caller, 200), advert.text
    # a push that is allowed is still refused (501) until pushing is implemented
    assert post.status_code == expected_write(repo, caller, 501), post.text
    if advert.status_code == 401:
        assert advert.headers["www-authenticate"] == 'Basic realm="git-frontend"'
        assert advert.headers["content-type"].startswith("text/plain")


async def test_the_answer_never_tells_a_private_repo_from_a_missing_one(callers):
    private, missing = REPOS["private model"], REPOS["missing model"]
    for caller, service in itertools.product(
        ("anonymous", "invalid Basic", "outsider", "visitor"),
        ("git-upload-pack", "git-receive-pack"),
    ):
        a = await callers(caller, "GET", f"{private.base}/info/refs?service={service}")
        b = await callers(caller, "GET", f"{missing.base}/info/refs?service={service}")
        shape = lambda r: (r.status_code, r.headers.get("content-type"), r.headers.get("www-authenticate"), r.text)
        assert shape(a) == shape(b), (caller, service)


# --- git: request validation and URL forms -----------------------------------


@pytest.mark.parametrize("query", ["", "?service=git-bad-service", "?service="])
async def test_a_missing_or_invalid_service_is_a_plain_400(callers, query):
    for repo_name in ("public model", "private model", "missing model"):
        response = await callers("anonymous", "GET", f"{REPOS[repo_name].base}/info/refs{query}")
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == "invalid service"


async def test_the_git_suffix_is_optional(callers):
    for base in ("/owner/demo-model", "/datasets/owner/e2e-public-dataset", "/spaces/owner/e2e-public-space"):
        response = await callers("anonymous", "GET", f"{base}/info/refs?service=git-upload-pack")
        assert response.status_code == 200, base
        with_suffix = await callers("anonymous", "GET", f"{base}.git/info/refs?service=git-upload-pack")
        assert response.content == with_suffix.content


async def test_a_dataset_is_reachable_with_and_without_its_prefix(callers):
    prefixed = await callers("anonymous", "GET", "/datasets/owner/e2e-public-dataset.git/info/refs?service=git-upload-pack")
    plain = await callers("anonymous", "GET", "/owner/e2e-public-dataset.git/info/refs?service=git-upload-pack")
    assert prefixed.status_code == plain.status_code == 200
    assert prefixed.content == plain.content


async def test_an_unknown_type_prefix_is_a_404(callers):
    response = await callers("anonymous", "GET", "/widgets/owner/demo-model.git/info/refs?service=git-upload-pack")
    assert response.status_code == 404


async def test_a_repository_is_only_found_under_its_own_type(callers):
    # a dataset asked for as a space (and the other way round) is not found
    wrong_type = await callers("owner Basic", "GET", "/spaces/owner/e2e-public-dataset.git/info/refs?service=git-upload-pack")
    assert wrong_type.status_code == 404


# --- LFS batch ---------------------------------------------------------------


def _lfs_error_checks(response, status):
    assert response.headers["content-type"].startswith("application/vnd.git-lfs+json")
    body = response.json()
    assert isinstance(body["message"], str) and body["message"]
    assert response.headers["x-error-message"] == body["message"]
    if status == 401:
        challenges = response.headers.get_list("lfs-authenticate")
        assert any(c.startswith("Basic") for c in challenges), challenges
        assert any(c.startswith("Bearer") for c in challenges), challenges


async def _batch(callers, caller, repo, operation):
    return await callers(
        caller,
        "POST",
        f"{repo.base}/info/lfs/objects/batch",
        content=json.dumps(
            {"operation": operation, "transfers": ["basic"], "objects": [{"oid": OID, "size": 3}]}
        ),
        headers=LFS_HEADERS,
    )


@pytest.mark.parametrize("repo_name,caller", MATRIX, ids=_ids(MATRIX))
async def test_lfs_download_follows_the_hub_rules(callers, repo_name, caller):
    repo = REPOS[repo_name]

    response = await _batch(callers, caller, repo, "download")

    want = expected_read(repo, caller)
    assert response.status_code == want, response.text
    if want == 200:
        assert response.headers["content-type"].startswith("application/vnd.git-lfs+json")
        (obj,) = response.json()["objects"]
        assert obj["error"]["code"] == 404  # no such object, reported per object
    else:
        _lfs_error_checks(response, want)


@pytest.mark.parametrize("repo_name,caller", MATRIX, ids=_ids(MATRIX))
async def test_lfs_upload_follows_the_hub_rules(callers, repo_name, caller):
    repo = REPOS[repo_name]

    response = await _batch(callers, caller, repo, "upload")

    want = expected_write(repo, caller, 200)
    assert response.status_code == want, response.text
    if want == 200:
        (obj,) = response.json()["objects"]
        assert obj["actions"]["upload"]["href"].startswith("http")
    else:
        _lfs_error_checks(response, want)


async def test_lfs_does_not_tell_a_private_repo_from_a_missing_one(callers):
    private, missing = REPOS["private model"], REPOS["missing model"]
    for caller, operation in itertools.product(("anonymous", "invalid Basic", "outsider"), ("download", "upload")):
        a = await _batch(callers, caller, private, operation)
        b = await _batch(callers, caller, missing, operation)
        shape = lambda r: (r.status_code, r.headers.get("content-type"), r.headers.get_list("lfs-authenticate"), r.json())
        assert shape(a) == shape(b), (caller, operation)
