"""The default brand icons are CheeseCave's own, not the upstream blue network mark."""

from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ["images/logo-square.svg", "images/logo-banner.svg", "images/logo-banner-dark.svg"]


def test_default_icons_carry_the_cheesecave_name_and_no_upstream_wordmark():
    for asset in ASSETS:
        text = (ROOT / asset).read_text(encoding="utf-8")
        assert "Kohaku" not in text, asset
        assert "CheeseCave" in text or "Cheese" in text, asset


def test_square_icon_is_a_square_svg():
    root = ET.fromstring((ROOT / "images/logo-square.svg").read_text(encoding="utf-8"))
    assert root.attrib["width"] == root.attrib["height"] == "512"
