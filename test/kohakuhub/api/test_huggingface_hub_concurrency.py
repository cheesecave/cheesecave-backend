"""Concurrency coverage for HF-compatible write paths.

``huggingface_hub.HfApi.upload_folder`` runs its preupload calls in parallel,
and real users push from CI runners that routinely fan out commits across
several machines. Those races are the kind only concurrent tests catch:

* Two clients call ``create_branch("feature-x")`` at the same millisecond.
* Two clients call ``create_repo("owner/foo")`` simultaneously.
* Two clients race an LFS preupload for the same content.

What we deliberately do **not** do here is force-translate infrastructure
errors (LakeFS 409 under contention, race-condition 500s) into prettier
HTTP codes. Those paths are genuine system-data-inconsistency situations,
and masking them would hide real bugs in the upstream flow. So the
assertions in this module are bounded by what actually matters:

* Data consistency — final state after the race is legal (at most one
  repo / branch / commit exists; storage is not corrupt).
* At least one participant made progress.
* Determinism — repeating the same race shape yields one of the
  documented outcomes rather than a hang or a silent no-op.

Whether the losing participant gets a 4xx or a 5xx is not asserted: the
raw error is what we want users to see, because that is the signal that
the operation *actually* conflicted and should not be retried blindly.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import RevisionNotFoundError

from test.kohakuhub.support.bootstrap import DEFAULT_PASSWORD


async def _owner_session(app) -> httpx.AsyncClient:
    """Build a fresh owner-authenticated httpx client.

    Concurrency tests need multiple independent sessions — a single client
    would serialize requests through its own connection pool. Each
    session logs in fresh so they run truly in parallel against the ASGI app.
    """
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    )
    response = await client.post(
        "/api/auth/login",
        json={"username": "owner", "password": DEFAULT_PASSWORD},
    )
    response.raise_for_status()
    return client


async def test_concurrent_create_repo_eventually_yields_single_repo(
    app, owner_client
):
    """Two parallel ``create_repo`` calls with the same name: one succeeds, the other
    answers the Hub's 409, and the system ends in a consistent state with exactly one
    repo on record.

    The loser used to be allowed a raw 5xx here. It is not (#87): the Hub serializes
    creation, so a concurrent create of one name is only ever a 409, and
    ``huggingface_hub``'s ``create_repo(exist_ok=True)`` takes nothing else as "it
    exists". The LakeFS errors a rival's create and cleanup cause (410 repository in
    deletion, a storage namespace being emptied) are that conflict, not a fault of ours.

    Only a server of its own threads shows the race: ``asyncio.gather`` over the ASGI app
    interleaves too little. Before the fix a round in about four had a 500.
    """
    client_a = await _owner_session(app)
    client_b = await _owner_session(app)
    try:
        body = {"type": "model", "name": "race-create-repo", "private": False}
        resp_a, resp_b = await asyncio.gather(
            client_a.post("/api/repos/create", json=body),
            client_b.post("/api/repos/create", json=body),
            return_exceptions=False,
        )
    finally:
        await client_a.aclose()
        await client_b.aclose()

    statuses = [resp_a.status_code, resp_b.status_code]
    assert sorted(statuses) == [200, 409], (statuses, resp_a.text[:200], resp_b.text[:200])  # #87
    success_count = sum(1 for s in statuses if 200 <= s < 300)
    assert success_count >= 1, (
        f"Expected at least one concurrent create_repo to succeed; got "
        f"{statuses} with bodies {resp_a.text[:200]!r} / {resp_b.text[:200]!r}"
    )

    # The final repo must be listable exactly once — the data-integrity
    # check that actually matters. If the race produced two rows or left
    # LakeFS without a DB record, subsequent reads would expose it here.
    listing = await owner_client.get(
        "/api/models?author=owner&search=race-create-repo"
    )
    assert listing.status_code == 200
    repos = [
        r for r in listing.json()
        if r.get("id") == "owner/race-create-repo"
    ]
    assert len(repos) == 1, (
        f"Concurrent create_repo produced an inconsistent listing: "
        f"{[r.get('id') for r in listing.json()]}"
    )


async def test_concurrent_create_branch_preserves_uniqueness(app, owner_client):
    """Parallel ``create_branch`` with the same name on the same repo.
    Invariant: exactly one branch record ends up in the final listing,
    and at least one caller received a success. The losing caller's exact
    error shape is intentionally not pinned — surfacing the raw upstream
    failure is more valuable for debugging than a polished 4xx.
    """
    await owner_client.post(
        "/api/repos/create",
        json={"type": "model", "name": "race-branch", "private": False},
    )

    client_a = await _owner_session(app)
    client_b = await _owner_session(app)
    try:
        create_body = {"branch": "contended-branch"}
        resp_a, resp_b = await asyncio.gather(
            client_a.post(
                "/api/models/owner/race-branch/branch/contended-branch",
                json=create_body,
            ),
            client_b.post(
                "/api/models/owner/race-branch/branch/contended-branch",
                json=create_body,
            ),
        )
    finally:
        await client_a.aclose()
        await client_b.aclose()

    statuses = [resp_a.status_code, resp_b.status_code]
    success_count = sum(1 for s in statuses if 200 <= s < 300)
    assert success_count >= 1, (
        f"Expected at least one concurrent create_branch to succeed; got "
        f"{statuses}: {resp_a.text[:150]!r} / {resp_b.text[:150]!r}"
    )

    # The branch must exist exactly once in the final ref listing.
    refs = await owner_client.get("/api/models/owner/race-branch/refs")
    assert refs.status_code == 200
    branch_names = [b["name"] for b in refs.json().get("branches", [])]
    assert branch_names.count("contended-branch") == 1, (
        f"Concurrent create_branch left an inconsistent ref listing: "
        f"{branch_names}"
    )


async def test_concurrent_preupload_same_file_returns_consistent_mode(
    app, owner_client
):
    """Two clients race ``preupload_lfs_files`` for the same path / sha.
    Both must receive an identical ``uploadMode`` decision (either both
    ``regular`` or both ``lfs``) — a race that returns different modes
    would cause one of the two clients to take the wrong branch and
    submit a malformed commit."""
    await owner_client.post(
        "/api/repos/create",
        json={"type": "model", "name": "race-preupload", "private": False},
    )

    client_a = await _owner_session(app)
    client_b = await _owner_session(app)
    try:
        body = {
            "files": [
                {
                    "path": "dup/shared.txt",
                    "size": 100,
                    "sha256": "c" * 64,
                }
            ]
        }
        resp_a, resp_b = await asyncio.gather(
            client_a.post(
                "/api/models/owner/race-preupload/preupload/main", json=body
            ),
            client_b.post(
                "/api/models/owner/race-preupload/preupload/main", json=body
            ),
        )
    finally:
        await client_a.aclose()
        await client_b.aclose()

    assert resp_a.status_code == 200 == resp_b.status_code, (
        f"Preupload race returned non-200: "
        f"{resp_a.status_code} / {resp_b.status_code}"
    )
    mode_a = resp_a.json()["files"][0]["uploadMode"]
    mode_b = resp_b.json()["files"][0]["uploadMode"]
    assert mode_a == mode_b, (
        f"Concurrent preupload returned inconsistent uploadMode: "
        f"{mode_a!r} vs {mode_b!r} — one client would commit through the "
        f"wrong path."
    )


async def test_concurrent_commits_to_same_branch_preserve_at_least_one_write(
    app, owner_client
):
    """Two clients each do a tiny commit to ``main`` at the same time.
    LakeFS serializes one and rejects the other with a branch-ref-changed
    style error. We pin that at least one commit made it into the tree
    and that the losing call surfaced *some* error (whatever shape).
    Masking the upstream LakeFS error into a friendly 4xx would rob a
    debugger of the signal that this specific race happened.
    """
    await owner_client.post(
        "/api/repos/create",
        json={"type": "model", "name": "race-commit", "private": False},
    )

    def _commit_body(path: str, text: str) -> str:
        import base64 as _b

        encoded = _b.b64encode(text.encode()).decode()
        return (
            '{"key":"header","value":{"summary":"race commit","description":""}}\n'
            + '{"key":"file","value":{"path":"'
            + path
            + '","content":"'
            + encoded
            + '","encoding":"base64"}}\n'
        )

    client_a = await _owner_session(app)
    client_b = await _owner_session(app)
    try:
        resp_a, resp_b = await asyncio.gather(
            client_a.post(
                "/api/models/owner/race-commit/commit/main",
                content=_commit_body("alpha.txt", "A"),
                headers={"Content-Type": "application/x-ndjson"},
            ),
            client_b.post(
                "/api/models/owner/race-commit/commit/main",
                content=_commit_body("beta.txt", "B"),
                headers={"Content-Type": "application/x-ndjson"},
            ),
        )
    finally:
        await client_a.aclose()
        await client_b.aclose()

    statuses = [resp_a.status_code, resp_b.status_code]
    success_count = sum(1 for s in statuses if 200 <= s < 300)
    assert success_count >= 1, (
        f"Expected at least one concurrent commit to succeed; got {statuses}: "
        f"{resp_a.text[:200]!r} / {resp_b.text[:200]!r}"
    )

    # At least one of the two new files must exist in the tree.
    tree = await owner_client.get("/api/models/owner/race-commit/tree/main")
    assert tree.status_code == 200
    paths = {entry["path"] for entry in tree.json()}
    # Exactly one will be present when LakeFS rejects the loser; both are
    # present only in the lucky "they both interleaved fine" outcome —
    # either is acceptable as long as we can see at least one write.
    assert paths & {"alpha.txt", "beta.txt"}, (
        f"Concurrent commits did not land any file in the tree: {paths}"
    )


@pytest.mark.hf_client
async def test_hf_api_create_repo_exist_ok_survives_concurrent_callers(live_server_url, hf_api_token):
    """Several processes calling ``create_repo(exist_ok=True)`` for one name together (a
    multi-worker upload, ``push_to_hub`` from every rank) must all return: a 500 made the
    losers raise."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    def create(name):
        return str(api.create_repo(f"owner/{name}", exist_ok=True))

    def rounds():
        for round_ in range(12):
            name = f"hf-race-{round_}"
            with ThreadPoolExecutor(6) as pool:
                urls = list(pool.map(create, [name] * 6))
            assert {u.rsplit("/", 1)[-1] for u in urls} == {name}
            assert api.repo_info(f"owner/{name}").id == f"owner/{name}"

    await asyncio.to_thread(rounds)


async def test_concurrent_deletes_of_one_branch_never_answer_a_5xx(app, owner_client):
    """Six at once: the branch goes, and no caller is told the server failed. LakeFS deletes
    idempotently inside the window of a race, so several callers may be told 200; the ones that
    come after it is gone are told the Hub's 404 RevisionNotFound. They were 500s."""
    response = await owner_client.post("/api/repos/create", json={"type": "model", "name": "race-delete-branch"})
    assert response.status_code == 200, response.text
    clients = [await _owner_session(app) for _ in range(6)]
    try:
        for round_ in range(4):
            branch = f"doomed-{round_}"
            created = await owner_client.post(
                "/api/models/owner/race-delete-branch/branch", json={"branch": branch, "revision": "main"}
            )
            assert created.status_code == 200, created.text
            responses = await asyncio.gather(
                *(c.delete(f"/api/models/owner/race-delete-branch/branch/{branch}") for c in clients)
            )
            statuses = [r.status_code for r in responses]
            assert set(statuses) <= {200, 404} and 200 in statuses, (statuses, [r.text[:120] for r in responses])
    finally:
        for c in clients:
            await c.aclose()
    refs = (await owner_client.get("/api/models/owner/race-delete-branch/refs")).json()
    assert [b["name"] for b in refs["branches"]] == ["main"]


@pytest.mark.hf_client
async def test_hf_api_delete_of_a_missing_branch_or_tag_raises_revision_not_found(live_server_url, hf_api_token):
    """``delete_branch`` and ``delete_tag`` document ``RevisionNotFoundError`` for a ref that does
    not exist (a second delete of one included); a 500 made them raise a generic server error."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    def run():
        api.create_repo("owner/hf-delete-missing", exist_ok=True)
        api.upload_file(path_or_fileobj=b"x", path_in_repo="a.txt", repo_id="owner/hf-delete-missing")
        api.create_branch("owner/hf-delete-missing", branch="once")
        api.create_tag("owner/hf-delete-missing", tag="v-once")
        api.delete_branch("owner/hf-delete-missing", branch="once")
        api.delete_tag("owner/hf-delete-missing", tag="v-once")
        for call, name in ((api.delete_branch, "once"), (api.delete_branch, "never"), (api.delete_tag, "v-once"), (api.delete_tag, "v-never")):
            kind = "branch" if call == api.delete_branch else "tag"
            with pytest.raises(RevisionNotFoundError):
                call("owner/hf-delete-missing", **{kind: name})

    await asyncio.to_thread(run)


@pytest.mark.hf_client
async def test_hf_api_concurrent_delete_branch_is_never_a_server_error(live_server_url, hf_api_token):
    """Several callers cleaning up the same branch (parallel CI jobs): the branch goes, and a caller
    is either told it was deleted or gets ``RevisionNotFoundError``, never a server error (LakeFS
    deletes idempotently inside the window of a race, so more than one may be told it was
    deleted). Only a server of its own threads shows the race; before the fix about every second
    round had a 500."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    def delete(branch):
        try:
            api.delete_branch("owner/hf-delete-race", branch=branch)
            return "deleted"
        except RevisionNotFoundError:
            return "not found"

    def rounds():
        api.create_repo("owner/hf-delete-race", exist_ok=True)
        api.upload_file(path_or_fileobj=b"x", path_in_repo="a.txt", repo_id="owner/hf-delete-race")
        for round_ in range(10):
            branch = f"doomed-{round_}"
            api.create_branch("owner/hf-delete-race", branch=branch)
            with ThreadPoolExecutor(6) as pool:
                outcomes = set(pool.map(delete, [branch] * 6))  # a 500 raises out of the thread
            assert "deleted" in outcomes and outcomes <= {"deleted", "not found"}
            assert branch not in [b.name for b in api.list_repo_refs("owner/hf-delete-race").branches]

    await asyncio.to_thread(rounds)
