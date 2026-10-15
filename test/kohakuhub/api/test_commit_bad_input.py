"""A commit that cannot be what it says is refused before anything is stored, and a revision that is not there is a 404 (#99).

Real LakeFS, MinIO and database. A body that is not base64 used to be stored as an empty file (``b64decode`` drops what is
not of the alphabet), a path with a ``..`` segment used to make a tree entry of that name, and a missing revision on the
commit list or on a merge used to be a 500.
"""

import base64
import json

import pytest

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}


async def _commit(client, operations, branch="main", summary="test"):
    lines = [{"key": "header", "value": {"summary": summary}}] + operations
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    return await client.post(f"{B}/commit/{branch}", content=body, headers=ND)


def _file(path, text="x", *, content=None):
    content = base64.b64encode(text.encode()).decode() if content is None else content
    return {"key": "file", "value": {"path": path, "content": content, "encoding": "base64"}}


async def _paths(client, ref="main"):
    return sorted(item["path"] for item in (await client.get(f"{B}/tree/{ref}?recursive=true")).json())


@pytest.fixture(autouse=True)
async def clean(owner_client):
    yield
    refs = (await owner_client.get(f"{B}/refs")).json()
    for branch in refs["branches"]:
        if branch["name"] != "main":
            await owner_client.delete(f"{B}/branch/{branch['name']}")
    await _commit(owner_client, [{"key": "deletedFolder", "value": {"path": "t99"}}])


# ----- the content of a file -----


@pytest.mark.parametrize("content", ["!!!", "aGVsbG8", "aGV$sbG8=", "not base64 at all"])
async def test_a_body_that_is_not_base64_is_refused_and_nothing_is_stored(owner_client, content):
    before = await _paths(owner_client)

    response = await _commit(owner_client, [_file("t99/ok.txt", "ok"), _file("t99/bad.txt", content=content)])

    assert response.status_code == 400, response.text
    assert "base64" in response.json()["detail"]["error"]
    assert await _paths(owner_client) == before  # not the file before it either


async def test_base64_wrapped_over_lines_is_still_a_body(owner_client):
    wrapped = "aGVs\nbG8g\r\nd29y bGQ="
    assert (await _commit(owner_client, [_file("t99/wrapped.txt", content=wrapped)])).status_code == 200

    tree = (await owner_client.get(f"{B}/tree/main/t99")).json()
    assert [(item["path"], item["size"]) for item in tree] == [("t99/wrapped.txt", len(b"hello world"))]


async def test_an_empty_body_is_an_empty_file(owner_client):
    """An empty string is valid base64 of nothing: unchanged."""
    assert (await _commit(owner_client, [_file("t99/empty.txt", content="")])).status_code == 200
    assert "t99/empty.txt" in await _paths(owner_client)


# ----- the path -----

DOTS = ["../escape.txt", "t99/../escape.txt", "t99/a/../../escape.txt", ".."]


@pytest.mark.parametrize("path", DOTS)
async def test_a_path_with_a_dot_dot_segment_is_refused_and_nothing_is_stored(owner_client, path):
    before = await _paths(owner_client)

    response = await _commit(owner_client, [_file("t99/first.txt"), _file(path)])

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"
    assert ".." in response.headers["x-error-message"]
    assert await _paths(owner_client) == before


async def test_a_copy_to_a_path_with_a_dot_dot_segment_is_refused(owner_client):
    assert (await _commit(owner_client, [_file("t99/src.txt", "s")])).status_code == 200
    copy = {"key": "copyFile", "value": {"path": "t99/../copy.txt", "srcPath": "t99/src.txt", "srcRevision": "main"}}

    response = await _commit(owner_client, [copy])

    assert response.status_code == 400, response.text
    assert ".." in response.headers["x-error-message"]  # ours, not what LakeFS made of the address
    assert "copy.txt" not in " ".join(await _paths(owner_client))


async def test_an_lfs_file_at_a_path_with_a_dot_dot_segment_is_refused(owner_client):
    lfs = {"key": "lfsFile", "value": {"path": "t99/../big.bin", "oid": "0" * 64, "size": 10, "algo": "sha256"}}

    response = await _commit(owner_client, [lfs])

    assert response.status_code == 400, response.text
    assert ".." in response.headers["x-error-message"]  # the path, not "never uploaded"


@pytest.mark.parametrize("path", ["t99/a..b.txt", "t99/..hidden", "t99/...", "t99/a./b", "t99/.gitkeep"])
async def test_names_that_only_contain_dots_are_kept(owner_client, path):
    assert (await _commit(owner_client, [_file(path)])).status_code == 200, path
    assert path in await _paths(owner_client)


async def test_a_file_that_is_already_there_with_such_a_path_can_still_be_deleted(owner_client):
    """Only what a commit puts somewhere is checked: a delete of a path is not refused."""
    delete = {"key": "deletedFile", "value": {"path": "t99/gone.txt"}}
    assert (await _commit(owner_client, [_file("t99/gone.txt")])).status_code == 200
    assert (await _commit(owner_client, [delete])).status_code == 200
    assert (await _commit(owner_client, [{"key": "deletedFile", "value": {"path": "../never.txt"}}])).status_code == 200


# ----- the rest of what a path may not be -----

# What no operation may carry: NUL made the database raise (an unhandled 500); the other controls are not names
CONTROLS = ["t99/a\x00b.txt", "t99/a\nb.txt", "t99/a\x1fb.txt", "t99/a\x7fb.txt", "\x00"]


@pytest.mark.parametrize("path", CONTROLS)
@pytest.mark.parametrize("key", ["file", "deletedFile", "deletedFolder"])
async def test_a_control_character_in_a_path_is_refused_whatever_the_operation(owner_client, path, key):
    before = await _paths(owner_client)
    operation = _file(path) if key == "file" else {"key": key, "value": {"path": path}}

    response = await _commit(owner_client, [_file("t99/first.txt"), operation])

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"
    assert "control character" in response.headers["x-error-message"]
    assert await _paths(owner_client) == before


@pytest.mark.parametrize("field", ["path", "srcPath"])
async def test_a_control_character_in_either_path_of_a_copy_is_refused(owner_client, field):
    assert (await _commit(owner_client, [_file("t99/src.txt", "s")])).status_code == 200
    value = {"path": "t99/copy.txt", "srcPath": "t99/src.txt", "srcRevision": "main"}
    value[field] = "t99/a\x00b.txt"

    response = await _commit(owner_client, [{"key": "copyFile", "value": value}])

    assert response.status_code == 400, response.text
    assert "control character" in response.headers["x-error-message"]


@pytest.mark.parametrize("path", [".", ".git", ".git/config", "t99/.git/x", "t99/.GIT/x", "t99/.Git", "t99/a/.git/b/c"])
async def test_a_path_that_is_a_dot_or_has_a_git_folder_is_refused_and_nothing_is_stored(owner_client, path):
    before = await _paths(owner_client)

    response = await _commit(owner_client, [_file("t99/first.txt"), _file(path)])

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"
    assert await _paths(owner_client) == before


@pytest.mark.parametrize("path", [".git/config", "t99/.git/x"])
async def test_a_copy_or_an_lfs_file_into_a_git_folder_is_refused(owner_client, path):
    assert (await _commit(owner_client, [_file("t99/src.txt", "s")])).status_code == 200
    copy = {"key": "copyFile", "value": {"path": path, "srcPath": "t99/src.txt", "srcRevision": "main"}}
    lfs = {"key": "lfsFile", "value": {"path": path, "oid": "0" * 64, "size": 10, "algo": "sha256"}}

    for operation in (copy, lfs):
        response = await _commit(owner_client, [operation])
        assert response.status_code == 400, response.text
        assert ".git" in response.headers["x-error-message"]  # the path, not "never uploaded"


@pytest.mark.parametrize(
    "path",
    [
        "t99/.gitignore",  # every repository has one of these
        "t99/.gitattributes",
        "t99/.github/workflows/ci.yml",
        "t99/.gitkeep",
        "t99/git/x",
        "t99/a.git/x",
        "t99/.cache/x",  # the client's own cache folder: no reason for a server to refuse it
        "t99/.hidden/x",
    ],
)
async def test_names_that_only_look_like_the_refused_ones_are_kept(owner_client, path):
    assert (await _commit(owner_client, [_file(path)])).status_code == 200, path
    assert path in await _paths(owner_client)


@pytest.mark.parametrize("path", [".git/config", "t99/.git/x", "t99/.GIT/y"])
@pytest.mark.parametrize("key", ["deletedFile", "deletedFolder"])
async def test_a_delete_of_a_path_in_a_git_folder_is_not_refused(owner_client, path, key):
    """Only what a commit puts somewhere is checked, so what an earlier commit stored can be cleared."""
    response = await _commit(owner_client, [_file("t99/first.txt"), {"key": key, "value": {"path": path}}])

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", [1, True, ["a"], {"a": 1}])
async def test_a_path_that_is_not_a_string_is_a_400(owner_client, path):
    response = await _commit(owner_client, [{"key": "file", "value": {"path": path, "content": "", "encoding": "base64"}}])

    assert response.status_code == 400, response.text
    assert "string" in response.headers["x-error-message"]


@pytest.mark.parametrize("value", ["a.txt", 1, ["a"], None])
async def test_an_operation_whose_value_is_not_an_object_is_a_400(owner_client, value):
    response = await _commit(owner_client, [{"key": "file", "value": value}])

    assert response.status_code == 400, response.text
    assert "object" in response.headers["x-error-message"]


# ----- the other places a path comes in -----


@pytest.mark.parametrize("path", ["a\x00b", "a\nb", ".git/config", "t99/.GIT/x", ".", "../x"])
async def test_preupload_refuses_a_path_a_commit_would_refuse(owner_client, path):
    response = await owner_client.post(f"{B}/preupload/main", json={"files": [{"path": path, "size": 1, "sha256": "0" * 64}]})

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"


async def test_preupload_still_answers_for_the_paths_a_client_sends(owner_client):
    files = [{"path": name, "size": 1, "sha256": "0" * 64} for name in ("t99/a.txt", "t99/.gitattributes", "t99/.github/x.yml")]

    response = await owner_client.post(f"{B}/preupload/main", json={"files": files})

    assert response.status_code == 200, response.text
    assert [f["path"] for f in response.json()["files"]] == [f["path"] for f in files]


@pytest.mark.parametrize("path", ["a\x00b", "a\nb"])
async def test_paths_info_refuses_a_control_character_in_a_path(owner_client, path):
    response = await owner_client.post(f"{B}/paths-info/main", data={"paths": ["README.md", path]})

    assert response.status_code == 400, response.text
    assert response.headers["x-error-code"] == "BadRequest"


async def test_paths_info_answers_for_a_path_in_a_git_folder_that_is_not_there(owner_client):
    """Only a control character is refused when a path is looked up: nothing is created."""
    response = await owner_client.post(f"{B}/paths-info/main", data={"paths": [".git/config", "README.md"]})

    assert response.status_code == 200, response.text
    assert [item["path"] for item in response.json()] == ["README.md"]


# ----- a revision that is not there -----


async def test_the_commits_of_a_revision_that_is_not_there_are_a_404(owner_client):
    response = await owner_client.get(f"{B}/commits/nope")

    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert "nope" in response.headers["x-error-message"]


async def test_the_commits_of_a_branch_that_is_there_are_listed(owner_client):
    response = await owner_client.get(f"{B}/commits/main")

    assert response.status_code == 200, response.text
    assert response.json()


@pytest.mark.parametrize("source,destination", [("nope", "main"), ("main", "nope"), ("nope", "nada")])
async def test_a_merge_with_a_branch_that_is_not_there_is_a_404(owner_client, source, destination):
    response = await owner_client.post(f"{B}/merge/{source}/into/{destination}", json={})

    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert "nope" in response.headers["x-error-message"] or "nada" in response.headers["x-error-message"]


async def test_a_merge_of_a_branch_that_is_there_still_works(owner_client):
    assert (await owner_client.post(f"{B}/branch", json={"branch": "t99-side", "revision": "main"})).status_code == 200
    assert (await _commit(owner_client, [_file("t99/side.txt", "s")], branch="t99-side")).status_code == 200

    response = await owner_client.post(f"{B}/merge/t99-side/into/main", json={})

    assert response.status_code == 200, response.text
    assert "t99/side.txt" in await _paths(owner_client)
