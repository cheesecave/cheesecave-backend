"""Fixtures and helpers shared by the downstream-library tests (no library is imported here).

A test file imports what it uses (``from ...downstream import endpoint, only_this_host, seeded``) and
guards itself with ``pytest.importorskip`` for the library it tests, so a missing library skips only
its own file.
"""

from __future__ import annotations

import asyncio
import socket
import sys

import pytest

from test.kohakuhub.support import hub_corpus


async def run(func, *args, **kwargs):
    """Library calls are blocking and the live server shares this loop's thread pool: run them aside."""
    return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture(autouse=True)
def only_this_host(monkeypatch):
    """A test that reaches another host is testing that host, not this server."""
    real = socket.socket.connect

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if sock.family != socket.AF_UNIX and host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"left the machine: {address!r}")
        return real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def endpoint(live_server_url, backend_test_state, monkeypatch):
    """The libraries take the hub from the environment, as users give it.

    ``HF_ENDPOINT`` is read when ``huggingface_hub`` is imported and the libraries copy it into their
    own modules, so every copy of the default hub (and the default client the module-level functions
    are bound to) is pointed at the server for this test.
    """
    import huggingface_hub.constants as constants
    from huggingface_hub import HfApi

    # The suite splits uploads into 500 KB parts to exercise the multipart path with a fake S3; real
    # MinIO (like S3) refuses parts under 5 MB, and real files are bigger than that. Use the defaults.
    app = backend_test_state.modules.config_module.cfg.app
    monkeypatch.setattr(app, "lfs_multipart_threshold_bytes", 100 * 1000 * 1000)
    monkeypatch.setattr(app, "lfs_multipart_chunk_size_bytes", 50 * 1000 * 1000)

    old = constants.ENDPOINT
    monkeypatch.setenv("HF_ENDPOINT", live_server_url)
    for name, module in list(sys.modules.items()):
        if not name.startswith(("huggingface_hub", "transformers", "datasets")):
            continue
        for attr, value in list(vars(module).items()):
            if isinstance(value, str) and old in value and attr.isupper():
                monkeypatch.setattr(module, attr, value.replace(old, live_server_url))
            elif isinstance(value, HfApi):
                monkeypatch.setattr(value, "endpoint", live_server_url)
    _forget_file_systems()
    yield live_server_url
    _forget_file_systems()  # a file system made for this server must not serve the next test's


def _forget_file_systems():
    """fsspec keeps the file systems it made, keyed by their arguments: the port of a past test's server."""
    from huggingface_hub import HfFileSystem

    HfFileSystem.clear_instance_cache()  # the cache is per class: clearing the base class's is not enough


@pytest.fixture
def seeded(endpoint, hf_api_token):
    """``await seeded(repo)``: the repository of the corpus as it is on the server (put there once)."""
    from huggingface_hub import HfApi

    api = HfApi(endpoint=endpoint, token=hf_api_token)

    async def put(repo: hub_corpus.CorpusRepo) -> str:
        return await run(hub_corpus.seed, api, repo)

    return put


def reference(call, *args, **kwargs):
    """What the library makes of the sampled files themselves, or a skip when it cannot read them.

    A library that cannot read a repository from disk (a newer architecture, a file format it does not
    know, an optional package it does not have) says nothing about the server either way: that case is
    skipped, with the reason, not failed.
    """
    try:
        return call(*args, **kwargs)
    except Exception as error:
        pytest.skip(f"the library cannot read the sampled files: {type(error).__name__}: {str(error)[:160]}")
