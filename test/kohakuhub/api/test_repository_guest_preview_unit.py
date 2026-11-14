"""Scoped guest previews use real SQL identities and replace only external storage."""

import asyncio
import base64
import csv
import io
import json
import struct
import zlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from kohakuhub import repository_invites as invites
from kohakuhub.api import repository_guest_preview as preview
from kohakuhub.api.repository_access import guest_cookie_name
from kohakuhub.db import Repository, RepositoryGuestSession, RepositoryInvite, utcnow
from test.kohakuhub.support.factories import make_file, make_repo, make_user

HEAD = "a" * 64
SOURCE_SECRET = "s3://private-bucket/raw-source?signature=private"


@pytest.fixture
def browser(db_fresh, monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "target", private=True, invite_mode=True, lakefs_repo="model-owner-target")
    sibling = make_repo(owner, "sibling", private=True)
    invitation, code = invites.issue_invite(repo, owner, expires_at=utcnow() + timedelta(days=1))
    guest, secret = invites.redeem_guest(repo, code)
    contents = {"README.md": b"# Controlled instructions\n"}
    metadata = {}
    storage = AsyncMock()
    storage.get_branch.return_value = {"commit_id": HEAD}

    async def stat(*, repository, ref, path, **kwargs):
        assert repository == "model-owner-target" and ref == HEAD
        if path not in contents:
            raise httpx.HTTPStatusError("internal secret", request=httpx.Request("GET", "https://storage.invalid"), response=httpx.Response(404))
        return {"path": path, "path_type": "object", "size_bytes": len(contents[path]),
                "physical_address": SOURCE_SECRET, "checksum": "private-checksum",
                "metadata": {"presigned_url": SOURCE_SECRET}, **metadata.get(path, {})}

    async def read(repository, ref, path, max_bytes):
        assert repository == "model-owner-target" and ref == HEAD
        return contents[path][:max_bytes]

    storage.stat_object.side_effect = stat
    storage.get_object_prefix.side_effect = read
    storage.list_objects.return_value = {"results": [], "pagination": {"has_more": False}}
    monkeypatch.setattr(preview, "get_lakefs_client", lambda: storage)
    app = FastAPI()
    app.include_router(preview.router, prefix="/api")
    with TestClient(app) as client:
        client.cookies.set(guest_cookie_name(repo), secret)
        yield SimpleNamespace(client=client, owner=owner, repo=repo, sibling=sibling,
                              invitation=invitation, guest=guest, secret=secret, storage=storage,
                              contents=contents, metadata=metadata, base="/api/models/owner/target/access/guest")


def get(browser, path="README.md", **kwargs):
    return browser.client.get(browser.base + "/preview", params={"path": path}, **kwargs)


def test_text_preview_uses_exact_scoped_main_snapshot_and_sanitized_envelope(browser):
    result = get(browser)
    assert result.status_code == 200
    assert result.json() == {"kind": "text", "text": "# Controlled instructions\n", "truncated": False}
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["vary"] == "Cookie"
    assert "location" not in result.headers
    for forbidden in (SOURCE_SECRET, "physical_address", "checksum", "presigned_url", HEAD):
        assert forbidden not in result.text
    browser.storage.get_branch.assert_awaited_once_with(repository="model-owner-target", branch="main")
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, "README.md", 8193)
    browser.storage.get_object.assert_not_awaited()


@pytest.mark.parametrize("content", [b"x" * 100000, b"line\n" * 1000, ("a" * 8191 + "汉字尾").encode()], ids=["byte-cap", "line-cap", "utf8-boundary"])
def test_text_is_a_fixed_prefix_bounded_by_bytes_and_lines(browser, content):
    browser.contents["README.md"] = content
    result = get(browser).json()
    assert result["kind"] == "text" and result["truncated"] is True
    assert len(result["text"].encode()) <= 8192
    assert len(result["text"].splitlines()) <= 64
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, "README.md", 8193)


@pytest.mark.parametrize("content", [b"\xff\xfe", b"binary\x00payload", b"\xe4"])
def test_binary_or_invalid_utf8_never_becomes_raw_text(browser, content):
    browser.contents["README.md"] = content
    result = get(browser)
    assert result.status_code == 200 and result.json()["kind"] == "unsupported"
    assert "text" not in result.json()


@pytest.mark.parametrize("path", ["data.parquet", "data.tar", "data.zip", "weights.safetensors", "image.svg", "unknown.bin"])
def test_unsupported_types_are_metadata_only_and_never_fetch_object_bytes(browser, path):
    browser.contents[path] = b"sensitive original bytes"
    result = get(browser, path)
    assert result.status_code == 200
    assert set(result.json()) == {"kind", "message", "size"}
    assert result.json()["kind"] == "unsupported" and result.json()["size"] == 24
    browser.storage.get_object_prefix.assert_not_awaited()


@pytest.mark.parametrize("lfs_source", ["row", "address", "pointer"])
def test_lfs_objects_are_not_downloaded_or_resolved_for_guests(browser, lfs_source):
    if lfs_source == "row":
        make_file(browser.repo, "README.md", "b" * 64, lfs=True)
    elif lfs_source == "address":
        browser.metadata["README.md"] = {"physical_address": "s3://bucket/lfs/aa/aa/" + "a" * 64}
    else:
        browser.contents["README.md"] = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 100000\n"
    result = get(browser)
    assert result.status_code == 200 and result.json()["kind"] == "unsupported"
    if lfs_source != "pointer":
        browser.storage.get_object_prefix.assert_not_awaited()
    assert "sha256" not in result.text
    browser.storage.get_object.assert_not_awaited()


@pytest.mark.parametrize("path", ["", "/README.md", "../README.md", "docs/../README.md", "./README.md", "docs//README.md", "docs\\README.md", "C:/README.md", "README.md\x00", "README.md\n", "x" * 2049], ids=["empty", "absolute", "parent", "nested-parent", "dot", "empty-segment", "backslash", "drive", "nul", "newline", "too-long"])
def test_invalid_paths_are_rejected_before_storage_access(browser, path):
    assert get(browser, path).status_code == 400
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("params", [{"ref": "old"}, {"revision": HEAD}, {"offset": 1}, {"cursor": "more"}, {"limit": 100}, {"fallback": "true"}])
def test_no_caller_controlled_revision_offset_or_range_is_accepted(browser, params):
    response = browser.client.get(browser.base + "/preview", params={"path": "README.md", **params})
    assert response.status_code == 400
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("header", ["bytes=0-4", "bytes=10-20", ""])
def test_range_headers_cannot_stitch_multiple_prefixes(browser, header):
    assert get(browser, headers={"Range": header}).status_code == 400
    browser.storage.get_branch.assert_not_awaited()


def test_duplicate_paths_are_rejected(browser):
    response = browser.client.get(browser.base + "/preview", params=[("path", "README.md"), ("path", "other.txt")])
    assert response.status_code == 400
    browser.storage.get_branch.assert_not_awaited()


@pytest.mark.parametrize("state", ["no-cookie", "expired", "revoked-session", "revoked-invite", "deleted-repo", "public", "other-repo"])
def test_every_request_checks_current_guest_scope_and_lifecycle(browser, state):
    if state == "no-cookie":
        browser.client.cookies.clear()
    elif state == "expired":
        RepositoryGuestSession.update(expires_at=utcnow() - timedelta(seconds=1)).where(RepositoryGuestSession.id == browser.guest.id).execute()
    elif state == "revoked-session":
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == browser.guest.id).execute()
    elif state == "revoked-invite":
        RepositoryInvite.update(revoked_at=utcnow()).where(RepositoryInvite.id == browser.invitation.id).execute()
    elif state == "deleted-repo":
        Repository.delete().where(Repository.id == browser.repo.id).execute()
    elif state == "public":
        Repository.update(private=False, invite_mode=False).where(Repository.id == browser.repo.id).execute()
    else:
        browser.base = "/api/models/owner/sibling/access/guest"
        browser.client.cookies.set(guest_cookie_name(browser.sibling), browser.secret)
    assert get(browser).status_code == 404
    assert browser.client.get(browser.base + "/tree").status_code == 404
    browser.storage.get_branch.assert_not_awaited()


def test_guest_cookie_is_required_even_with_normal_login_and_login_cookie_is_preserved(browser):
    browser.client.cookies.set("session_id", "existing-account-session")
    assert get(browser).status_code == 200
    assert browser.client.cookies.get("session_id") == "existing-account-session"
    browser.client.cookies.delete(guest_cookie_name(browser.repo))
    assert get(browser).status_code == 404
    assert browser.client.cookies.get("session_id") == "existing-account-session"


@pytest.mark.parametrize("endpoint", ["tree", "preview"])
def test_missing_and_unauthorized_private_repositories_are_identical(browser, endpoint):
    browser.client.cookies.clear()
    existing = browser.client.get(browser.base + "/" + endpoint, params={"path": "README.md"})
    missing = browser.client.get("/api/models/owner/missing/access/guest/" + endpoint, params={"path": "README.md"})
    assert existing.status_code == missing.status_code == 404
    assert existing.content == missing.content
    assert existing.headers["x-error-code"] == missing.headers["x-error-code"]
    browser.storage.get_branch.assert_not_awaited()


def test_deleted_file_and_other_repository_file_cannot_be_previewed(browser):
    make_file(browser.repo, "README.md", "c" * 40, is_deleted=True)
    make_file(browser.sibling, "only-other.txt", "d" * 40)
    assert get(browser).status_code == 404
    assert get(browser, "only-other.txt").status_code == 404
    browser.storage.get_object_prefix.assert_not_awaited()


@pytest.mark.parametrize("path,delimiter", [("table.csv", ","), ("table.tsv", "\t")])
def test_tabular_preview_caps_rows_columns_and_cells(browser, path, delimiter):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow([f"column-{i}" for i in range(25)])
    for row in range(30):
        writer.writerow(["x" * 300 if column == 0 else str(row) for column in range(25)])
    browser.contents[path] = stream.getvalue().encode()
    payload = get(browser, path).json()
    assert payload["kind"] == "rows" and payload["truncated"] is True
    assert len(payload["columns"]) == len(payload["rows"]) == 20
    assert all(len(row) == 20 and all(len(cell) <= 256 for cell in row) for row in payload["rows"])
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, path, 65537)


def test_jsonl_preview_unifies_first_rows_without_returning_raw_lines(browser):
    browser.contents["table.jsonl"] = b'{"name":"one","value":1}\n{"name":"two","enabled":true}\n'
    assert get(browser, "table.jsonl").json() == {
        "kind": "rows", "columns": ["name", "value", "enabled"],
        "rows": [["one", "1", ""], ["two", "", "true"]], "truncated": False,
    }


@pytest.mark.parametrize("path,content", [("bad.jsonl", b"not-json\n"), ("bad.jsonl", b"[1,2,3]\n"), ("bad.csv", b"\xff"), ("bad.csv", b'header\n"unterminated')])
def test_malformed_data_is_unsupported_without_raw_fallback(browser, path, content):
    browser.contents[path] = content
    assert get(browser, path).json()["kind"] == "unsupported"


def test_incomplete_large_jsonl_tail_is_discarded_and_input_stays_bounded(browser):
    browser.contents["table.jsonl"] = b'{"first":"safe"}\n' + b'{"huge":"' + b"x" * 100000
    result = get(browser, "table.jsonl").json()
    assert result == {"kind": "rows", "columns": ["first"], "rows": [["safe"]], "truncated": True}
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, "table.jsonl", 65537)


def png_bytes(size=(1000, 600)):
    data = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("source", SOURCE_SECRET)
    Image.new("RGB", size, "red").save(data, format="PNG", pnginfo=metadata)
    return data.getvalue()


def test_thumbnail_is_resized_reencoded_and_strips_original_metadata(browser):
    original = png_bytes()
    browser.contents["photo.png"] = original
    payload = get(browser, "photo.png").json()
    assert payload["kind"] == "image" and max(payload["width"], payload["height"]) == 512
    assert payload["data_url"].startswith("data:image/png;base64,")
    output = base64.b64decode(payload["data_url"].split(",", 1)[1])
    assert output != original and SOURCE_SECRET.encode() not in output
    with Image.open(io.BytesIO(output)) as image:
        assert image.size == (payload["width"], payload["height"])
        assert not image.info and not image.getexif()
    browser.storage.get_object_prefix.assert_awaited_once_with("model-owner-target", HEAD, "photo.png", 2097153)


@pytest.mark.parametrize("fmt,suffix", [("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif")])
def test_raster_types_are_reencoded_as_single_png_frame(browser, fmt, suffix):
    data = io.BytesIO()
    first = Image.new("RGB", (30, 20), "red")
    if fmt == "GIF":
        first.save(data, format=fmt, save_all=True, append_images=[Image.new("RGB", (30, 20), "blue")], duration=100, loop=0)
    else:
        first.save(data, format=fmt)
    browser.contents[f"photo.{suffix}"] = data.getvalue()
    payload = get(browser, f"photo.{suffix}").json()
    assert payload["kind"] == "image"
    with Image.open(io.BytesIO(base64.b64decode(payload["data_url"].split(",", 1)[1]))) as image:
        assert image.format == "PNG" and getattr(image, "n_frames", 1) == 1
        assert image.getpixel((0, 0))[0] > 200 and image.getpixel((0, 0))[2] < 10


@pytest.mark.parametrize("kind", ["oversize", "pixels", "malformed", "incomplete"])
def test_unsafe_images_remain_metadata_only(browser, kind):
    content = png_bytes((1, 1))
    if kind == "oversize":
        browser.metadata["photo.png"] = {"size_bytes": 2097153}
    elif kind == "pixels":
        dimensions = struct.pack(">II", 5000, 4000)
        header = content[12:16] + dimensions + content[24:29]
        content = content[:16] + dimensions + content[24:29] + struct.pack(">I", zlib.crc32(header)) + content[33:]
    elif kind == "malformed":
        content = b"not-an-image"
    else:
        browser.metadata["photo.png"] = {"size_bytes": len(content) + 100}
    browser.contents["photo.png"] = content
    result = get(browser, "photo.png").json()
    assert result["kind"] == "unsupported" and "data_url" not in result
    if kind == "oversize":
        browser.storage.get_object_prefix.assert_not_awaited()


def test_tree_strips_metadata_and_lists_only_one_bounded_level(browser):
    browser.storage.list_objects.return_value = {"results": [
        {"path": "docs/README.md", "path_type": "object", "size_bytes": 10, "physical_address": SOURCE_SECRET, "checksum": "secret"},
        {"path": "docs/nested/", "path_type": "common_prefix", "physical_address": SOURCE_SECRET},
        {"path": "docs/deleted.txt", "path_type": "object", "size_bytes": 10},
        {"path": "docs/weights.bin", "path_type": "object", "size_bytes": 10000},
    ], "pagination": {"has_more": True, "next_offset": "private-cursor"}}
    make_file(browser.repo, "docs/deleted.txt", "e" * 40, is_deleted=True)
    payload = browser.client.get(browser.base + "/tree", params={"path": "docs"})
    assert payload.status_code == 200
    assert payload.json()["entries"] == [
        {"path": "docs/README.md", "type": "file", "size": 10, "preview_kind": "text"},
        {"path": "docs/nested", "type": "directory", "size": 0, "preview_kind": None},
        {"path": "docs/weights.bin", "type": "file", "size": 10000, "preview_kind": "unsupported"},
    ]
    assert payload.json()["path"] == "docs" and payload.json()["has_more"] is True
    assert payload.json()["next_cursor"]
    for secret in (SOURCE_SECRET, "checksum", "private-cursor", "next_offset", "deleted.txt"):
        assert secret not in payload.text
    browser.storage.list_objects.assert_awaited_once_with(repository="model-owner-target", ref=HEAD, prefix="docs/", delimiter="/", amount=100)
    browser.storage.get_object_prefix.assert_not_awaited()


def test_tree_rejects_unbounded_offsets_and_entries_outside_requested_prefix(browser):
    assert browser.client.get(browser.base + "/tree", params={"offset": 100}).status_code == 400
    browser.storage.list_objects.return_value = {"results": [
        {"path": "other/private.txt", "path_type": "object", "size_bytes": 20},
    ]}
    response = browser.client.get(browser.base + "/tree", params={"path": "docs"})
    assert response.status_code == 503 and "other/private.txt" not in response.text


def test_tree_is_capped_even_when_storage_returns_too_many_entries(browser):
    browser.storage.list_objects.return_value = {"results": [
        {"path": f"file-{index}.txt", "path_type": "object", "size_bytes": 2} for index in range(130)
    ]}
    result = browser.client.get(browser.base + "/tree").json()
    assert len(result["entries"]) == 100 and result["has_more"] is True


@pytest.mark.parametrize("operation", ["get_branch", "stat_object", "get_object_prefix", "list_objects"])
def test_storage_failures_are_generic_and_never_fall_back(browser, operation):
    getattr(browser.storage, operation).side_effect = RuntimeError(SOURCE_SECRET)
    response = browser.client.get(browser.base + "/tree") if operation == "list_objects" else get(browser)
    assert response.status_code == 503 and SOURCE_SECRET not in response.text
    assert "location" not in response.headers


def test_storage_calls_have_a_bounded_timeout(browser, monkeypatch):
    async def stalled(**kwargs):
        await asyncio.sleep(1)
    browser.storage.get_branch.side_effect = stalled
    monkeypatch.setattr(preview, "STORAGE_TIMEOUT_SECONDS", 0.001)
    assert get(browser).status_code == 503


def test_revocation_while_storage_is_reading_discards_the_preview(browser):
    async def read_and_revoke(*args):
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id == browser.guest.id).execute()
        return b"# Must not leave the server"
    browser.storage.get_object_prefix.side_effect = read_and_revoke
    response = get(browser)
    assert response.status_code == 404 and "Must not" not in response.text


def test_empty_snapshot_has_no_files_and_invalid_snapshot_fails_closed(browser):
    browser.storage.get_branch.return_value = {"commit_id": None}
    assert browser.client.get(browser.base + "/tree").json() == {"entries": [], "path": "", "has_more": False}
    assert get(browser).status_code == 404
    browser.storage.stat_object.assert_not_awaited()
    browser.storage.get_branch.return_value = {"commit_id": "../../another-repository"}
    response = get(browser)
    assert response.status_code == 503 and "another-repository" not in response.text


@pytest.mark.parametrize("entries", [None, [None], [{"path_type": "unknown"}], [{"path_type": "object", "path": "../outside"}]])
def test_malformed_listings_fail_without_returning_external_metadata(browser, entries):
    browser.storage.list_objects.return_value = {"results": entries}
    response = browser.client.get(browser.base + "/tree")
    assert response.status_code == 503 and "outside" not in response.text


def test_tree_deduplicates_and_does_not_expand_a_storage_ignored_delimiter(browser):
    browser.storage.list_objects.return_value = {"results": [
        {"path": "README.md", "path_type": "object", "size_bytes": 20},
        {"path": "README.md", "path_type": "object", "size_bytes": 20},
        {"path": "nested/file.txt", "path_type": "object", "size_bytes": 20},
        {"path": "photo.png", "path_type": "object", "size_bytes": -1},
        {"path": "large.txt", "path_type": "object", "size_bytes": 20000},
    ]}
    make_file(browser.repo, "large.txt", "e" * 64, lfs=True)
    result = browser.client.get(browser.base + "/tree").json()
    assert [entry["path"] for entry in result["entries"]] == ["README.md", "photo.png", "large.txt"]
    assert result["entries"][1]["size"] is None and result["entries"][1]["preview_kind"] == "unsupported"
    assert result["entries"][2]["preview_kind"] == "unsupported"


@pytest.mark.parametrize("metadata", [{"path": "other.txt"}, {"path_type": "common_prefix"}])
def test_stat_cannot_redirect_preview_to_another_path_or_directory(browser, metadata):
    browser.metadata["README.md"] = metadata
    assert get(browser).status_code == 404
    browser.storage.get_object_prefix.assert_not_awaited()


def test_non_bytes_storage_content_fails_without_serializing_payload(browser):
    browser.storage.get_object_prefix.side_effect = None
    browser.storage.get_object_prefix.return_value = {"url": SOURCE_SECRET}
    response = get(browser)
    assert response.status_code == 503 and SOURCE_SECRET not in response.text


def test_prefix_response_is_locally_bounded_even_if_storage_contract_is_broken(browser):
    browser.storage.get_object_prefix.side_effect = None
    browser.storage.get_object_prefix.return_value = b"x" * 100000
    payload = get(browser).json()
    assert payload == {"kind": "text", "text": "x" * 8192, "truncated": True}


def test_guest_previews_keep_working_for_private_mode_without_overwriting_login(browser):
    Repository.update(invite_mode=False).where(Repository.id == browser.repo.id).execute()
    browser.client.cookies.set("session_id", "ordinary-account-cookie")
    assert get(browser).status_code == 200
    assert browser.client.get(browser.base + "/tree").status_code == 200
    assert browser.client.cookies.get("session_id") == "ordinary-account-cookie"


def test_revocation_during_tree_listing_discards_entries(browser):
    async def revoke(**kwargs):
        RepositoryInvite.update(revoked_at=utcnow()).where(RepositoryInvite.id == browser.invitation.id).execute()
        return {"results": [{"path": "must-not-leave.txt", "path_type": "object", "size_bytes": 2}]}
    browser.storage.list_objects.side_effect = revoke
    response = browser.client.get(browser.base + "/tree")
    assert response.status_code == 404 and "must-not-leave" not in response.text


def test_thumbnail_worker_timeout_is_a_generic_failure(browser, monkeypatch):
    browser.contents["photo.png"] = png_bytes((1, 1))
    async def stalled(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(preview.asyncio, "to_thread", stalled)
    monkeypatch.setattr(preview, "STORAGE_TIMEOUT_SECONDS", 0.001)
    response = get(browser, "photo.png")
    assert response.status_code == 503 and SOURCE_SECRET not in response.text


@pytest.mark.parametrize("path,kind", [("README", "text"), ("example.py", "text"), ("x.JSONL", "rows"), ("x.PNG", "image"), ("x.svg", "unsupported")])
def test_preview_type_classification_is_explicit(path, kind):
    assert preview._kind(path, 100) == kind


def test_data_helpers_keep_empty_tables_and_pad_short_rows():
    assert preview._rows(b"", 0, ".csv") == {"kind": "rows", "columns": [], "rows": [], "truncated": False}
    assert preview._rows(b"a,b\n1\n", 6, ".csv")["rows"] == [["1", ""]]
    text = "\n" + "\n".join(json.dumps({f"column-{column}": {"value": "x" * 300} if column == 0 else column for column in range(25)}) for _ in range(30))
    result = preview._rows(text.encode(), len(text.encode()), ".jsonl")
    assert result["truncated"] is True
    assert len(result["columns"]) == len(result["rows"]) == 20
    assert all(len(cell) <= 256 for row in result["rows"] for cell in row)
    assert preview._cell("x" * 300) == ("x" * 256, True)


def test_non_allowed_raster_formats_cannot_use_an_image_extension():
    data = io.BytesIO()
    Image.new("RGB", (1, 1)).save(data, format="BMP")
    assert preview._thumbnail(data.getvalue()) is None


@pytest.mark.parametrize("path", ["document.pdf", "video.mp4", "audio.wav"])
def test_media_types_attempt_controlled_rendering_and_fail_closed_for_invalid_sources(browser, path):
    browser.contents[path] = b"sensitive original bytes"
    result = get(browser, path)
    assert result.status_code == 422
    assert "sensitive original bytes" not in result.text
    browser.storage.get_object_prefix.assert_awaited_once()
    browser.storage.get_object.assert_not_awaited()
