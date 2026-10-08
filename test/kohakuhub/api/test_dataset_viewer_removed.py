"""The dataset viewer is removed: no routes, no package, nothing to switch on.

The component carried its own non-commercial license and was not wanted any
more. It is gone; these tests keep it from coming back by accident.
"""

from __future__ import annotations

import importlib.util

import pytest

VIEWER_ROUTES = [
    ("POST", "/api/dataset-viewer/preview"),
    ("POST", "/api/dataset-viewer/tar/list"),
    ("POST", "/api/dataset-viewer/tar/extract"),
    ("POST", "/api/dataset-viewer/tar/webdataset"),
    ("POST", "/api/dataset-viewer/sql"),
    ("GET", "/api/dataset-viewer/rate-limit"),
    ("GET", "/api/dataset-viewer/health"),
]


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:  # the parent package itself does not exist
        return False


def test_the_package_is_gone():
    # Look for the modules, not the directory: an empty leftover directory (only
    # a __pycache__) in an old checkout is a harmless namespace package.
    for module in ("router", "parsers", "sql_query", "http_file"):
        assert not _importable(f"kohakuhub.datasetviewer.{module}")


def test_no_route_mentions_the_viewer(app):
    # app.routes holds included routers as opaque objects; the OpenAPI schema
    # lists every mounted path flattened.
    paths = list(app.openapi()["paths"])

    assert len(paths) > 50, "the schema should list the whole API"
    assert [path for path in paths if "dataset-viewer" in path] == []


@pytest.mark.parametrize("method, path", VIEWER_ROUTES)
async def test_the_viewer_urls_answer_404(client, method, path):
    response = await client.request(method, path, json={"url": "http://127.0.0.1/"})

    assert response.status_code == 404
