"""The real ``git`` and ``git-lfs`` commands against a live server.

These run the actual binaries, as ``hfutils.hf_hub_clone`` does: credentials
embedded in the URL, ``GIT_LFS_SKIP_SMUDGE`` for pointer-only clones, ``-b`` and
``--depth``. A client sends credentials only after a ``401`` with a challenge,
so each case checks what ends up on disk or what git says, not only a status.

Roles come from the shared baseline: ``owner`` (public model ``owner/demo-model``
and organization ``acme-labs``), ``member`` (admin of ``acme-labs``), ``visitor``
(read-only in ``acme-labs``), ``outsider`` (no access).
"""

from __future__ import annotations

import hashlib
import os

import pytest
import pytest_asyncio
from huggingface_hub import HfApi

from test.kohakuhub.support.git_clients import (
    GitClient,
    git_available,
    lfs_available,
    with_credentials,
)

pytestmark = [
    pytest.mark.skipif(not git_available(), reason="git is not installed"),
]
needs_lfs = pytest.mark.skipif(not lfs_available(), reason="git-lfs is not installed")

# above the test LFS threshold (1 KiB) so it is an LFS object, below the multipart one (1 MB)
BIG = os.urandom(300_000)
BIG_OID = hashlib.sha256(BIG).hexdigest()
README = b"# e2e\n"

PUBLIC = "owner/e2e-git-public"
PRIVATE = "owner/e2e-git-private"
DATASET = "owner/e2e-git-dataset"
SPACE = "owner/e2e-git-space"


async def _token(client):
    response = await client.post("/api/auth/tokens/create", json={"name": "git-e2e"})
    response.raise_for_status()
    return response.json()["token"]


def _seed(api, repo_id, repo_type, private, with_big):
    if api.repo_exists(repo_id, repo_type=repo_type):
        return
    extra = {"space_sdk": "static"} if repo_type == "space" else {}
    api.create_repo(repo_id, repo_type=repo_type, private=private, **extra)
    api.upload_file(path_or_fileobj=README, path_in_repo="README.md", repo_id=repo_id, repo_type=repo_type)
    if with_big:
        api.upload_file(path_or_fileobj=BIG, path_in_repo="weights.bin", repo_id=repo_id, repo_type=repo_type)


@pytest_asyncio.fixture
async def hub(live_server_url, owner_client, member_client, visitor_client, outsider_client, tmp_path):
    tokens = {
        "owner": await _token(owner_client),
        "member": await _token(member_client),
        "visitor": await _token(visitor_client),
        "outsider": await _token(outsider_client),
    }
    api = HfApi(endpoint=live_server_url, token=tokens["owner"])
    _seed(api, PUBLIC, "model", False, True)
    _seed(api, PRIVATE, "model", True, True)
    _seed(api, DATASET, "dataset", False, True)
    _seed(api, SPACE, "space", False, True)

    class Hub:
        url = live_server_url

        def __init__(self):
            self.git = GitClient(tmp_path / "home")
            self.dest = tmp_path / "work"
            self.dest.mkdir()
            self.tokens = tokens

        def repo_url(self, repo_id, repo_type="model", as_user=None):
            prefix = "" if repo_type == "model" else f"/{repo_type}s"
            url = f"{live_server_url}{prefix}/{repo_id}.git"
            return with_credentials(url, as_user, tokens[as_user]) if as_user else url

        def clone(self, name, repo_id, repo_type="model", as_user=None, *args, skip_smudge=False):
            result = self.git.clone(self.repo_url(repo_id, repo_type, as_user), self.dest / name, *args, skip_smudge=skip_smudge)
            return result, self.dest / name

    return Hub()


def _is_pointer(path, size=None, oid=None):
    text = path.read_bytes()[:300].decode()
    lines = text.splitlines()
    return (
        lines[0] == "version https://git-lfs.github.com/spec/v1"
        and lines[1] == f"oid sha256:{oid or BIG_OID}"
        and lines[2] == f"size {size or len(BIG)}"
    )


# --- public repositories ------------------------------------------------------


@needs_lfs
def test_a_public_repo_clones_anonymously_with_its_lfs_file(hub):
    result, work = hub.clone("full", PUBLIC)

    assert result.returncode == 0, result.output
    assert (work / "README.md").read_bytes() == README
    assert (work / "weights.bin").read_bytes() == BIG


@needs_lfs
def test_a_pointer_only_clone_keeps_pointers_and_lfs_pull_fills_them(hub):
    result, work = hub.clone("pointer", PUBLIC, skip_smudge=True)

    assert result.returncode == 0, result.output
    assert _is_pointer(work / "weights.bin")
    assert (work / "README.md").read_bytes() == README
    listed = hub.git.run(["git", "lfs", "ls-files"], cwd=work)
    assert "weights.bin" in listed.stdout
    assert hub.git.run(["git", "lfs", "fsck", "--pointers"], cwd=work).returncode == 0

    pulled = hub.git.run(["git", "lfs", "pull"], cwd=work)
    assert pulled.returncode == 0, pulled.output
    assert (work / "weights.bin").read_bytes() == BIG


@needs_lfs
def test_lfs_fetch_include_brings_only_what_is_asked_for(hub):
    _, work = hub.clone("include", PUBLIC, skip_smudge=True)

    fetched = hub.git.run(["git", "lfs", "pull", "--include=weights.bin"], cwd=work)

    assert fetched.returncode == 0, fetched.output
    assert (work / "weights.bin").read_bytes() == BIG


def test_a_public_repo_ignores_invalid_credentials(hub):
    url = with_credentials(hub.repo_url(PUBLIC), "x", "hf_notatoken")

    result = hub.git.clone(url, hub.dest / "invalid", skip_smudge=True)

    assert result.returncode == 0, result.output  # treated as anonymous, never challenged


def test_ls_remote_lists_the_default_branch(hub):
    result = hub.git.run(["git", "ls-remote", hub.repo_url(PUBLIC)])

    assert result.returncode == 0, result.output
    assert "refs/heads/main" in result.stdout


# --- private repositories ----------------------------------------------------


@needs_lfs
def test_a_private_repo_clones_with_the_token_in_the_url(hub):
    result, work = hub.clone("private", PRIVATE, as_user="owner")

    assert result.returncode == 0, result.output
    assert (work / "README.md").read_bytes() == README
    assert (work / "weights.bin").read_bytes() == BIG  # the LFS download used the same credentials


@needs_lfs
def test_a_private_pointer_only_clone_pulls_later_with_the_same_credentials(hub):
    result, work = hub.clone("private-pointer", PRIVATE, as_user="owner", skip_smudge=True)

    assert result.returncode == 0, result.output
    assert _is_pointer(work / "weights.bin")
    pulled = hub.git.run(["git", "lfs", "pull"], cwd=work)
    assert pulled.returncode == 0, pulled.output
    assert (work / "weights.bin").read_bytes() == BIG


def test_without_credentials_git_asks_for_them(hub):
    result, _ = hub.clone("anon-private", PRIVATE, skip_smudge=True)

    assert result.returncode != 0
    # a challenge made git ask; with prompting disabled that is the message it gives
    assert "terminal prompts disabled" in result.output or "could not read Username" in result.output, result.output


def test_a_wrong_token_is_refused_after_git_sent_it(hub):
    url = with_credentials(hub.repo_url(PRIVATE), "owner", "hf_notatoken")

    result = hub.git.clone(url, hub.dest / "wrong", skip_smudge=True)

    assert result.returncode != 0
    assert "Authentication failed" in result.output, result.output


def test_a_user_without_access_is_told_the_repo_is_not_found(hub):
    result, _ = hub.clone("outsider", PRIVATE, as_user="outsider", skip_smudge=True)

    assert result.returncode != 0
    assert "not found" in result.output, result.output


def test_a_bearer_credential_works_for_the_git_routes_too(hub):
    # (git-lfs would send this extra header on to the storage host too, so the LFS side of
    # Bearer is covered by the batch tests in test_git_auth_matrix.py instead)
    token = hub.tokens["owner"]
    result = hub.git.run(
        ["git", "-c", f"http.extraHeader=Authorization: Bearer {token}", "clone", hub.repo_url(PRIVATE), str(hub.dest / "bearer")],
        skip_smudge=True,
    )

    assert result.returncode == 0, result.output
    assert (hub.dest / "bearer" / "README.md").read_bytes() == README


def test_org_members_read_a_private_dataset_and_outsiders_cannot(hub):
    for role in ("member", "visitor"):
        result, work = hub.clone(f"acme-{role}", "acme-labs/private-dataset", "dataset", as_user=role, skip_smudge=True)
        assert result.returncode == 0, (role, result.output)
        assert any(entry.name != ".git" for entry in work.iterdir()), "the clone has files"

    refused, _ = hub.clone("acme-outsider", "acme-labs/private-dataset", "dataset", as_user="outsider", skip_smudge=True)
    assert refused.returncode != 0
    assert "not found" in refused.output


# --- datasets, Spaces and how hfutils calls git -------------------------------


@needs_lfs
def test_a_dataset_clones_through_its_type_prefix_with_its_lfs_file(hub):
    result, work = hub.clone("dataset", DATASET, "dataset")

    assert result.returncode == 0, result.output
    assert (work / "README.md").read_bytes() == README
    assert (work / "weights.bin").read_bytes() == BIG  # the LFS download followed the dataset URL


@needs_lfs
def test_a_space_clones_through_its_type_prefix_with_its_lfs_file(hub):
    result, work = hub.clone("space", SPACE, "space")

    assert result.returncode == 0, result.output
    assert (work / "README.md").read_bytes() == README
    assert (work / "weights.bin").read_bytes() == BIG


@needs_lfs
def test_a_dataset_pointer_only_clone_pulls_later(hub):
    result, work = hub.clone("dataset-pointer", DATASET, "dataset", skip_smudge=True)
    assert result.returncode == 0, result.output
    assert _is_pointer(work / "weights.bin")

    pulled = hub.git.run(["git", "lfs", "pull"], cwd=work)

    assert pulled.returncode == 0, pulled.output
    assert (work / "weights.bin").read_bytes() == BIG


def test_the_repo_does_not_carry_a_pinned_lfs_endpoint(hub):
    # git-lfs derives <remote>/info/lfs from the clone URL, which is right for every
    # URL form; a file in the tree naming one server and one path is not
    for name, repo_id, repo_type in (("m", PUBLIC, "model"), ("d", DATASET, "dataset")):
        _, work = hub.clone(f"nolfsconfig-{name}", repo_id, repo_type, skip_smudge=True)
        assert not (work / ".lfsconfig").exists(), repo_type


def test_the_hfutils_call_shape_works(hub):
    # hf_hub_clone(no_lfs=True, revision="main") on a dataset, token in the URL
    result, work = hub.clone("hfutils", DATASET, "dataset", "owner", "-b", "main", skip_smudge=True)

    assert result.returncode == 0, result.output
    assert (work / "README.md").read_bytes() == README
    assert _is_pointer(work / "weights.bin")


# --- pushing ----------------------------------------------------------------


def test_a_push_is_refused_clearly_and_changes_nothing(hub):
    result, work = hub.clone("push", PRIVATE, as_user="owner", skip_smudge=True)
    assert result.returncode == 0, result.output
    before = hub.git.run(["git", "ls-remote", hub.repo_url(PRIVATE, as_user="owner")]).stdout
    (work / "new.txt").write_text("hello\n")
    hub.git.run(["git", "add", "new.txt"], cwd=work)
    hub.git.run(["git", "commit", "-m", "x"], cwd=work)

    pushed = hub.git.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=work)

    assert pushed.returncode != 0
    assert "not supported" in pushed.output or "501" in pushed.output, pushed.output
    assert hub.git.run(["git", "ls-remote", hub.repo_url(PRIVATE, as_user="owner")]).stdout == before
