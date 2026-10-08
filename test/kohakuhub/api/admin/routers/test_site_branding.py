"""Branding API regression tests on the shared persistent real-database fixture."""

import base64
from io import BytesIO
from pathlib import Path
import random
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from peewee import OperationalError
from PIL import Image
import pytest

from kohakuhub.api.admin.routers.site_branding import router as admin_router
from kohakuhub.api.misc import router as misc_router
from kohakuhub.api.site_branding import router as public_router
from kohakuhub.config import cfg
from kohakuhub.db import SiteBranding
from kohakuhub import site_branding
from kohakuhub import gif_branding
from kohakuhub import db as db_module

ADMIN_URL = "/admin/api/site-branding"
PUBLIC_URL = "/api/site-branding"
TOKEN = "branding-regression-token"
HEADERS = {"X-Admin-Token": TOKEN}


@pytest.fixture
def branding_client(db_fresh, monkeypatch):
    # Requests run on TestClient's worker threads, so rows must be committed:
    # db_fresh is a file-backed database, not a rolled-back transaction.
    monkeypatch.setattr(cfg.admin, "enabled", True)
    monkeypatch.setattr(cfg.admin, "secret_token", TOKEN)
    monkeypatch.setattr(cfg.app, "site_name", "Configured Hub")
    test_app = FastAPI()
    test_app.include_router(public_router, prefix="/api")
    test_app.include_router(misc_router, prefix="/api")
    test_app.include_router(admin_router, prefix="/admin/api")
    with TestClient(test_app, raise_server_exceptions=False) as session:
        yield session, db_fresh


def image_bytes(size=(640, 320), image_format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", size, (20, 80, 160)).save(buffer, format=image_format)
    return buffer.getvalue()


def upload(session, kind, contents=None):
    return session.post(
        f"{ADMIN_URL}/assets/{kind}",
        headers=HEADERS,
        files={"file": ("logo.png", contents or image_bytes(), "image/png")},
    )


def test_defaults_and_text_override_persist_across_connections(branding_client):
    session, database = branding_client
    expected = site_branding.default_branding()
    assert session.get(PUBLIC_URL).json() == expected
    assert SiteBranding.select().count() == 0
    response = session.put(
        ADMIN_URL,
        headers=HEADERS,
        json={"site_name": "  Example Hub  ", "footer_description": "Custom footer"},
    )
    assert response.status_code == 200
    expected.update(site_name="Example Hub", footer_description="Custom footer")
    # Close and reopen the file: the saved configuration is on disk, not in memory.
    database.close()
    database.connect()
    assert site_branding.get_branding() == expected
    assert session.get(PUBLIC_URL).json() == expected
    assert session.get(ADMIN_URL, headers=HEADERS).json() == expected
    assert SiteBranding.select().count() == 1
    assert (
        session.put(ADMIN_URL, headers=HEADERS, json={"footer_description": ""}).json()[
            "footer_description"
        ]
        == ""
    )
    assert session.get(PUBLIC_URL).json()["site_name"] == "Example Hub"
    config = session.get("/api/site-config").json()
    assert config["site_name"] == "Example Hub"
    assert "header_logo" not in config and "favicon" not in config
    # The separate API product-identification endpoint keeps its original behavior.
    assert session.get("/api/version").json()["name"] == "Configured Hub"


def test_assets_are_independent_normalized_and_resettable(branding_client):
    session, _ = branding_client
    first = upload(session, "header_logo", image_bytes(image_format="JPEG"))
    assert first.status_code == 200
    header = first.json()["header_logo"]
    assert header.startswith("data:image/png;base64,")
    with Image.open(BytesIO(base64.b64decode(header.split(",", 1)[1]))) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (512, 256)
    assert first.json()["favicon"] is None
    favicon_response = upload(session, "favicon")
    assert favicon_response.status_code == 200
    favicon = favicon_response.json()["favicon"]
    with Image.open(BytesIO(base64.b64decode(favicon.split(",", 1)[1]))) as normalized:
        assert normalized.size == (256, 128)
    assert favicon_response.json()["header_logo"] == header
    text = session.put(ADMIN_URL, headers=HEADERS, json={"site_name": "Brand Hub"}).json()
    assert text["header_logo"] == header and text["favicon"] == favicon
    reset = session.delete(f"{ADMIN_URL}/assets/header_logo", headers=HEADERS)
    assert reset.status_code == 200
    assert reset.json()["header_logo"] is None
    assert reset.json()["favicon"] == favicon
    assert session.get(PUBLIC_URL).json() == reset.json()
    assert session.delete(f"{ADMIN_URL}/assets/favicon", headers=HEADERS).json()["favicon"] is None


@pytest.mark.parametrize("image_format", ["WEBP", "ICO"])
def test_other_supported_raster_formats_are_reencoded(branding_client, image_format):
    session, _ = branding_client
    response = upload(session, "favicon", image_bytes((64, 64), image_format))
    assert response.status_code == 200
    data_url = response.json()["favicon"]
    with Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (64, 64)


def test_admin_disabled_rejects_branding_access(branding_client, monkeypatch):
    session, _ = branding_client
    monkeypatch.setattr(cfg.admin, "enabled", False)
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 503
    assert session.get(PUBLIC_URL).status_code == 200


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", ""),
        ("put", ""),
        ("post", "/assets/header_logo"),
        ("delete", "/assets/favicon"),
        ("patch", "/assets/header_logo/animation"),
    ],
)
def test_every_admin_operation_requires_auth(branding_client, method, path):
    session, _ = branding_client
    kwargs = {"json": {"site_name": "Hub"}} if method == "put" else {}
    if method == "patch":
        kwargs["json"] = {"loop": True}
    if method == "post":
        kwargs["files"] = {"file": ("logo.png", image_bytes(), "image/png")}
    assert session.request(method, ADMIN_URL + path, **kwargs).status_code == 401
    assert (
        session.request(
            method, ADMIN_URL + path, headers={"X-Admin-Token": "incorrect"}, **kwargs
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"site_name": " "},
        {"site_name": "a" * 101},
        {"site_name": None},
        {"footer_description": "a" * 2001},
        {"footer_description": None},
        {"header_logo": "data:image/png;base64,evil"},
    ],
)
def test_invalid_text_and_direct_asset_overrides_rejected(branding_client, payload):
    session, _ = branding_client
    assert session.put(ADMIN_URL, headers=HEADERS, json=payload).status_code == 422
    assert session.get(PUBLIC_URL).json() == site_branding.default_branding()


@pytest.mark.parametrize(
    "contents", [b"not an image", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>']
)
def test_invalid_and_active_image_content_rejected(branding_client, contents):
    session, _ = branding_client
    assert upload(session, "header_logo", contents).status_code == 400
    assert session.get(PUBLIC_URL).json()["header_logo"] is None


def svg_bytes(content="", attributes=""):
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}>{content}</svg>'.encode()


def decode_svg(data_url):
    assert data_url.startswith("data:image/svg+xml;base64,")
    return base64.b64decode(data_url.split(",", 1)[1])


def test_actual_repository_svg_is_preserved_independent_and_resettable(branding_client):
    session, database = branding_client
    source = (Path(__file__).parents[5] / "images" / "logo-square.svg").read_bytes()
    response = upload(session, "header_logo", source)
    assert response.status_code == 200, response.text
    header = response.json()["header_logo"]
    normalized = ET.fromstring(decode_svg(header))
    original = ET.fromstring(source)
    assert normalized.attrib == original.attrib
    assert [(node.tag, node.attrib) for node in normalized.iter()] == [
        (node.tag, node.attrib) for node in original.iter()
    ]
    assert response.json()["favicon"] is None
    favicon = upload(session, "favicon", svg_bytes('<path d="M0 0L10 10"/>')).json()["favicon"]
    assert favicon != header
    assert session.put(ADMIN_URL, headers=HEADERS, json={"site_name": "Vector Hub"}).json() == {
        **site_branding.default_branding(),
        "site_name": "Vector Hub",
        "header_logo": header,
        "favicon": favicon,
    }
    database.close()
    database.connect()
    public = session.get(PUBLIC_URL)
    assert public.json()["header_logo"] == header
    assert public.json()["favicon"] == favicon
    assert "X-Site-Branding-Fallback" not in public.headers
    reset = session.delete(f"{ADMIN_URL}/assets/header_logo", headers=HEADERS).json()
    assert reset["header_logo"] is None
    assert reset["favicon"] == favicon
    assert session.delete(f"{ADMIN_URL}/assets/favicon", headers=HEADERS).json()["favicon"] is None


@pytest.mark.parametrize("kind", ["header_logo", "favicon"])
def test_svg_utf8_and_static_vector_features(branding_client, kind):
    session, _ = branding_client
    source = svg_bytes(
        '<defs><linearGradient id="gradient"><stop offset="0" stop-color="red"/></linearGradient>'
        '<path id="shape" d="M0 0L10 10"/><clipPath id="clip"><circle r="4"/></clipPath>'
        '<mask id="mask"><rect width="10" height="10" fill="white"/></mask>'
        '<filter id="blur"><feGaussianBlur stdDeviation="1"/></filter></defs>'
        '<style>.logo {fill: url("#gradient"); stroke: rgb(20, 80, 160);}</style>'
        '<g clip-path="url(#clip)" mask="url(#mask)" filter="url(#blur)">'
        '<use xlink:href="#shape" class="logo" style="opacity: .8"/>'
        '<text xml:lang="zh" font-family="sans-serif">站点<tspan>Logo</tspan></text></g>',
        'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10"',
    )
    source = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n' + source
    response = upload(session, kind, source)
    assert response.status_code == 200, response.text
    normalized = decode_svg(response.json()[kind])
    assert "站点" in normalized.decode("utf-8")
    assert ET.fromstring(normalized).attrib["viewBox"] == "0 0 10 10"


@pytest.mark.parametrize(
    "source",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg">',
        b"<svg/>",
        b'<html xmlns="http://www.w3.org/2000/svg"/>',
        b'<svg xmlns="https://evil.example/svg"/>',
        b'<!DOCTYPE svg [<!ENTITY payload "entity">]><svg xmlns="http://www.w3.org/2000/svg">&payload;</svg>',
        b'<!DOCTYPE svg [<!ENTITY payload SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&payload;</svg>',
        b'<?xml-stylesheet href="https://evil.example/style.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
        svg_bytes("<script>alert(1)</script>"),
        svg_bytes("<foreignObject><div/></foreignObject>"),
        svg_bytes('<animate attributeName="href" values="https://evil.example"/>'),
        svg_bytes('<set attributeName="onload" to="alert(1)"/>'),
        svg_bytes('<image href="https://evil.example/logo.png"/>'),
        svg_bytes('<circle onload="alert(1)"/>'),
        svg_bytes('<use href="https://evil.example/logo.svg#shape"/>'),
        svg_bytes('<use href="data:image/svg+xml;base64,PHN2Zy8+"/>'),
        svg_bytes('<use href="javascript:alert(1)"/>'),
        svg_bytes('<use href="&#104;ttps://evil.example/logo.svg"/>'),
        svg_bytes(
            '<use xlink:href="//evil.example/logo.svg"/>',
            'xmlns:xlink="http://www.w3.org/1999/xlink"',
        ),
        svg_bytes('<g xml:base="https://evil.example/"><use href="#shape"/></g>'),
        svg_bytes("<evil:g/>", 'xmlns:evil="https://evil.example/ns"'),
        svg_bytes('<circle style="fill: url(https://evil.example/image.svg)"/>'),
        svg_bytes('<circle fill="url(//evil.example/image.svg)"/>'),
        svg_bytes('<style>@import "https://evil.example/style.css";</style>'),
        svg_bytes(
            "<style>@font-face {font-family: evil; src: url(https://evil.example/font);}</style>"
        ),
        svg_bytes("<style>.a {fill: u\\72l(https://evil.example/image.svg);}</style>"),
        svg_bytes('<style>.a {fill: url("\\68ttps://evil.example/image.svg");}</style>'),
        svg_bytes("<style>.a {fill: url(https://evil.example/);}</style>"),
        svg_bytes('<style>@\\69mport "https://evil.example/style.css";</style>'),
        svg_bytes("<style>.a {fill: expression(alert(1));}</style>"),
        svg_bytes("<style>.a {--remote: url(https://evil.example); fill: var(--remote);}</style>"),
        svg_bytes("<style>.a {fill: url(#shape;}</style>"),
        svg_bytes('<style>.a {fill: url("#shape") url("https://evil.example");}</style>'),
    ],
)
def test_svg_invalid_xml_and_active_or_external_content_rejected(branding_client, source):
    session, _ = branding_client
    response = upload(session, "header_logo", source)
    assert response.status_code == 400, response.text
    assert session.get(PUBLIC_URL).json()["header_logo"] is None


def test_svg_size_and_complexity_limits(branding_client):
    session, _ = branding_client
    oversized = svg_bytes("<text>" + "a" * site_branding.MAX_ASSET_BYTES + "</text>")
    assert upload(session, "header_logo", oversized).status_code == 400
    # Source comments do not contribute to the normalized asset/cache limit.
    compact = svg_bytes("<!--" + "a" * site_branding.MAX_ASSET_BYTES + '--><circle r="10"/>')
    response = upload(session, "header_logo", compact)
    assert response.status_code == 200
    assert len(decode_svg(response.json()["header_logo"])) < 256
    complex_svg = svg_bytes("<g>" * 66 + "</g>" * 66)
    assert upload(session, "favicon", complex_svg).status_code == 400
    nested_css = "calc(" * 65 + "1" + ")" * 65
    assert (
        upload(session, "favicon", svg_bytes(f'<path stroke-width="{nested_css}"/>')).status_code
        == 400
    )


def test_svg_mime_is_detected_from_content(branding_client):
    session, _ = branding_client
    response = session.post(
        f"{ADMIN_URL}/assets/favicon",
        headers=HEADERS,
        files={"file": ("anything.jpg", svg_bytes('<circle r="2"/>'), "application/octet-stream")},
    )
    assert response.status_code == 200
    decode_svg(response.json()["favicon"])


def test_image_limits_and_unknown_asset_kinds(branding_client):
    session, _ = branding_client
    assert (
        upload(session, "header_logo", b"x" * (site_branding.MAX_UPLOAD_BYTES + 1)).status_code
        == 413
    )
    assert upload(session, "header_logo", image_bytes((4001, 4000))).status_code == 400
    noise = Image.frombytes("RGB", (512, 512), random.Random(7).randbytes(512 * 512 * 3))
    output = BytesIO()
    noise.save(output, "PNG")
    assert upload(session, "header_logo", output.getvalue()).status_code == 400
    assert upload(session, "unknown").status_code == 422
    assert session.delete(f"{ADMIN_URL}/assets/unknown", headers=HEADERS).status_code == 422
    assert session.get(PUBLIC_URL).json() == site_branding.default_branding()


def test_public_database_failure_defaults_are_marked_but_admin_failure_visible(
    branding_client, monkeypatch
):
    # Deliberate outage: the request runs on a worker thread, where a rolled-back
    # DROP TABLE would not be visible, so the failing read is injected here.
    session, _ = branding_client

    def unavailable(*args, **kwargs):
        raise OperationalError("test database unavailable")

    monkeypatch.setattr(SiteBranding, "get_or_none", unavailable)
    response = session.get(PUBLIC_URL)
    assert response.status_code == 200
    assert response.json() == site_branding.default_branding()
    assert response.headers["X-Site-Branding-Fallback"] == "true"
    assert response.headers["Cache-Control"] == "no-store"
    assert session.get(ADMIN_URL, headers=HEADERS).status_code == 500


def test_startup_creates_branding_table_without_a_destructive_migration(monkeypatch):
    database = Mock()
    monkeypatch.setattr(db_module, "db", database)
    db_module.init_db()
    database.connect.assert_called_once_with(reuse_if_open=True)
    assert SiteBranding in database.create_tables.call_args.args[0]
    assert database.create_tables.call_args.kwargs == {"safe": True}


def animated_gif_bytes(size=(64, 32), loop=4):
    output = BytesIO()
    frames = [Image.new("RGBA", size, color) for color in ["red", "blue", "green"]]
    frames[0].save(
        output, "GIF", save_all=True, append_images=frames[1:], duration=[70, 130, 210], loop=loop
    )
    return output.getvalue()


@pytest.mark.parametrize("loop", [True, False])
def test_gif_frames_timing_resize_and_upload_playback(branding_client, loop):
    kind, edge = "header_logo", 512
    session, _ = branding_client
    response = session.post(
        f"{ADMIN_URL}/assets/{kind}",
        headers=HEADERS,
        files={"file": ("logo.gif", animated_gif_bytes((640, 320)), "image/gif")},
        data={"loop": str(loop).lower()},
    )
    assert response.status_code == 200, response.text
    asset = response.json()[kind]
    assert asset.startswith("data:image/gif;base64,")
    with Image.open(BytesIO(base64.b64decode(asset.split(",", 1)[1]))) as gif:
        assert gif.format == "GIF"
        assert gif.size == (edge, edge // 2)
        assert gif.n_frames == 3
        assert gif.info.get("loop") == (0 if loop else None)
        for index, (duration, color) in enumerate(
            zip([70, 130, 210], [(255, 0, 0), (0, 0, 255), (0, 128, 0)])
        ):
            gif.seek(index)
            assert gif.info["duration"] == duration
            assert gif.convert("RGB").getpixel((edge // 2, edge // 4)) == color
    assert session.get(PUBLIC_URL).json()[kind] == asset


def test_gif_default_repeat_and_lossless_playback_patch(branding_client):
    kind = "header_logo"
    session, _ = branding_client
    session.put(ADMIN_URL, headers=HEADERS, json={"site_name": "Animated Hub"})
    other_kind = "favicon" if kind == "header_logo" else "header_logo"
    other_asset = upload(session, other_kind).json()[other_kind]
    before = upload(session, kind, animated_gif_bytes()).json()
    before_bytes = base64.b64decode(before[kind].split(",", 1)[1])
    with Image.open(BytesIO(before_bytes)) as gif:
        assert gif.info["loop"] == 0
    once = session.patch(
        f"{ADMIN_URL}/assets/{kind}/animation", headers=HEADERS, json={"loop": False}
    )
    assert once.status_code == 200, once.text
    assert once.json()["site_name"] == "Animated Hub"
    assert once.json()[other_kind] == other_asset
    once_bytes = base64.b64decode(once.json()[kind].split(",", 1)[1])
    extension = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    assert once_bytes == before_bytes.replace(extension, b"", 1)
    with Image.open(BytesIO(once_bytes)) as gif:
        assert "loop" not in gif.info
        assert gif.n_frames == 3
    again = session.patch(
        f"{ADMIN_URL}/assets/{kind}/animation", headers=HEADERS, json={"loop": True}
    )
    assert again.status_code == 200
    assert again.json()[kind] == before[kind]
    assert session.get(PUBLIC_URL).json() == again.json()


@pytest.mark.parametrize("loop", [True, False])
def test_gif_favicon_upload_uses_static_first_frame(branding_client, loop):
    session, _ = branding_client
    logo = upload(session, "header_logo", animated_gif_bytes()).json()["header_logo"]
    response = session.post(
        f"{ADMIN_URL}/assets/favicon",
        headers=HEADERS,
        files={"file": ("favicon.gif", animated_gif_bytes((640, 320)), "image/gif")},
        data={"loop": str(loop).lower()},
    )
    assert response.status_code == 200
    favicon = response.json()["favicon"]
    assert favicon.startswith("data:image/png;base64,")
    with Image.open(BytesIO(base64.b64decode(favicon.split(",", 1)[1]))) as image:
        assert image.format == "PNG"
        assert image.n_frames == 1
        assert image.size == (256, 128)
        assert image.convert("RGB").getpixel((128, 64)) == (255, 0, 0)
    assert SiteBranding.get_by_id(1).favicon == favicon
    assert session.get(PUBLIC_URL).json()["favicon"] == favicon
    assert response.json()["header_logo"] == logo
    assert (
        session.patch(
            f"{ADMIN_URL}/assets/favicon/animation", headers=HEADERS, json={"loop": not loop}
        ).status_code
        == 400
    )
    assert session.get(PUBLIC_URL).json()["favicon"] == favicon


def test_legacy_gif_favicon_is_static_without_rewriting_saved_data(branding_client):
    session, _ = branding_client
    legacy = "data:image/gif;base64," + base64.b64encode(animated_gif_bytes()).decode()
    SiteBranding.create(id=1, site_name="Existing Hub", header_logo=legacy, favicon=legacy)
    for path, headers in [(PUBLIC_URL, {}), (ADMIN_URL, HEADERS)]:
        response = session.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["site_name"] == "Existing Hub"
        assert response.json()["header_logo"] == legacy
        asset = response.json()["favicon"]
        assert asset.startswith("data:image/png;base64,")
        with Image.open(BytesIO(base64.b64decode(asset.split(",", 1)[1]))) as image:
            assert image.n_frames == 1
            assert image.convert("RGB").getpixel((32, 16)) == (255, 0, 0)
    assert SiteBranding.get_by_id(1).favicon == legacy
    assert (
        session.patch(
            f"{ADMIN_URL}/assets/favicon/animation", headers=HEADERS, json={"loop": False}
        ).status_code
        == 400
    )
    assert SiteBranding.get_by_id(1).favicon == legacy


def test_gif_favicon_first_frame_transparency_is_preserved(branding_client):
    session, _ = branding_client
    first = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
    first.paste((255, 0, 0, 255), (0, 0, 10, 10))
    second = Image.new("RGBA", (20, 10), (0, 0, 255, 255))
    source = BytesIO()
    first.save(source, "GIF", save_all=True, append_images=[second], duration=100, disposal=2)
    response = upload(session, "favicon", source.getvalue())
    assert response.status_code == 200
    with Image.open(
        BytesIO(base64.b64decode(response.json()["favicon"].split(",", 1)[1]))
    ) as image:
        assert image.getpixel((15, 5))[3] == 0
        assert image.getpixel((5, 5)) == (255, 0, 0, 255)


def test_invalid_legacy_gif_favicon_falls_back_without_losing_other_branding(branding_client):
    session, _ = branding_client
    SiteBranding.create(id=1, site_name="Existing Hub", favicon="data:image/gif;base64,broken")
    response = session.get(PUBLIC_URL)
    assert response.status_code == 200
    assert response.json()["site_name"] == "Existing Hub"
    assert response.json()["favicon"] is None


def test_gif_transparency_and_disposal_are_preserved(branding_client):
    session, _ = branding_client
    frames = []
    for left in [True, False]:
        frame = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
        frame.paste((255, 0, 0, 255), (0 if left else 10, 0, 10 if left else 20, 10))
        frames.append(frame)
    source = BytesIO()
    frames[0].save(
        source, "GIF", save_all=True, append_images=frames[1:], duration=[100, 200], disposal=2
    )
    response = upload(session, "header_logo", source.getvalue())
    assert response.status_code == 200, response.text
    with Image.open(
        BytesIO(base64.b64decode(response.json()["header_logo"].split(",", 1)[1]))
    ) as gif:
        gif.seek(0)
        assert gif.convert("RGBA").getpixel((15, 5))[3] == 0
        assert gif.convert("RGBA").getpixel((5, 5)) == (255, 0, 0, 255)
        gif.seek(1)
        assert gif.convert("RGBA").getpixel((5, 5))[3] == 0
        assert gif.convert("RGBA").getpixel((15, 5)) == (255, 0, 0, 255)


def test_gif_accumulated_partial_frames_are_composited(branding_client):
    session, _ = branding_client
    first = Image.new("RGBA", (20, 10), (255, 0, 0, 255))
    second = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
    second.paste((0, 0, 255, 255), (10, 0, 20, 10))
    source = BytesIO()
    first.save(
        source, "GIF", save_all=True, append_images=[second], duration=[100, 200], disposal=1
    )
    response = upload(session, "header_logo", source.getvalue())
    assert response.status_code == 200, response.text
    with Image.open(
        BytesIO(base64.b64decode(response.json()["header_logo"].split(",", 1)[1]))
    ) as gif:
        gif.seek(1)
        assert gif.convert("RGBA").getpixel((5, 5)) == (255, 0, 0, 255)
        assert gif.convert("RGBA").getpixel((15, 5)) == (0, 0, 255, 255)


@pytest.mark.parametrize(
    "payload", [{}, {"loop": "false"}, {"loop": None}, {"loop": 0}, {"loop": True, "extra": 1}]
)
def test_gif_playback_patch_validation(branding_client, payload):
    session, _ = branding_client
    assert (
        session.patch(
            f"{ADMIN_URL}/assets/header_logo/animation", headers=HEADERS, json=payload
        ).status_code
        == 422
    )


def test_gif_playback_patch_requires_existing_gif(branding_client):
    session, _ = branding_client
    path = f"{ADMIN_URL}/assets/header_logo/animation"
    assert session.patch(path, headers=HEADERS, json={"loop": False}).status_code == 400
    png = upload(session, "header_logo").json()["header_logo"]
    assert session.patch(path, headers=HEADERS, json={"loop": True}).status_code == 400
    assert session.get(PUBLIC_URL).json()["header_logo"] == png
    assert (
        session.patch(
            f"{ADMIN_URL}/assets/unknown/animation", headers=HEADERS, json={"loop": True}
        ).status_code
        == 422
    )
    assert (
        session.post(
            f"{ADMIN_URL}/assets/favicon",
            headers=HEADERS,
            files={"file": ("logo.gif", animated_gif_bytes(), "image/gif")},
            data={"loop": "invalid"},
        ).status_code
        == 422
    )


def test_gif_frame_and_total_decode_limits(branding_client, monkeypatch):
    session, _ = branding_client
    monkeypatch.setattr(gif_branding, "MAX_GIF_FRAMES", 2)
    assert upload(session, "header_logo", animated_gif_bytes()).status_code == 400
    monkeypatch.setattr(gif_branding, "MAX_GIF_FRAMES", 200)
    monkeypatch.setattr(gif_branding, "MAX_GIF_TOTAL_PIXELS", 64 * 32 * 3 - 1)
    assert upload(session, "header_logo", animated_gif_bytes()).status_code == 400
    assert session.get(PUBLIC_URL).json()["header_logo"] is None


def test_gif_normalized_size_limit(branding_client, monkeypatch):
    session, _ = branding_client
    monkeypatch.setattr(site_branding, "MAX_ASSET_BYTES", 100)
    assert upload(session, "header_logo", animated_gif_bytes()).status_code == 400


@pytest.mark.parametrize("source", [b"GIF89a", b"GIF89a" + b"\x00" * 20, animated_gif_bytes()[:-1]])
def test_invalid_gif_loop_changes_are_rejected(source):
    with pytest.raises(HTTPException) as error:
        gif_branding.set_gif_loop(source, True)
    assert error.value.status_code == 400
