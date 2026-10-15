"""The README card a Space is created with (#95, A): what the Hub writes for ``space_sdk``."""

import pytest

from kohakuhub import repo_card
from kohakuhub.space_card import SPACE_SDKS, space_card


def front_matter(sdk, name="demo"):
    return repo_card.load_front_matter(space_card(name, sdk))


@pytest.mark.parametrize("sdk", ["gradio", "streamlit", "docker", "static"])
def test_a_space_with_a_known_sdk_gets_the_card_the_hub_writes(sdk):
    assert front_matter(sdk) == {
        "title": "demo",
        "emoji": "🚀",
        "colorFrom": "blue",
        "colorTo": "green",
        "sdk": sdk,
        "pinned": False,
    }


def test_the_sdks_are_the_ones_huggingface_hub_documents():
    assert SPACE_SDKS == ("gradio", "streamlit", "docker", "static")


@pytest.mark.parametrize("sdk", [None, "", "flask", "Gradio", " gradio", 3, ["gradio"]])
def test_no_sdk_or_an_unknown_one_gets_no_card(sdk):
    """Inventing an sdk would be a guess: the Space is created without a card, as before."""
    assert space_card("demo", sdk) is None


@pytest.mark.parametrize("name", ["123", "true", "null", "a:b", "yes", "0x1f", "a #b", "-lead"])
def test_a_name_that_yaml_would_read_as_something_else_stays_the_title(name):
    assert front_matter("static", name)["title"] == name


def test_the_card_is_a_readme_the_card_checks_accept():
    errors, warnings = repo_card.check_readme(space_card("demo", "gradio").decode())

    assert errors == [] and warnings == []


def test_the_card_points_to_the_configuration_reference():
    text = space_card("demo", "static").decode()

    assert text.startswith("---\n") and "spaces-config-reference" in text.split("---\n", 2)[2]
