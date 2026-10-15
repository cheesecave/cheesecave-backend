"""The README card a Space is created with.

The Hub writes ``README.md`` with the front matter of the Space (its ``sdk`` among it) when a Space
is created with ``space_sdk``; ``space_info().sdk``, the lists and the facets read it from there.
A Space created without a known sdk gets no card: inventing one would be a guess.
"""

from __future__ import annotations

import yaml

SPACE_SDKS = ("gradio", "streamlit", "docker", "static")


def space_card(name: str, sdk) -> bytes | None:
    """The ``README.md`` of a new Space called ``name`` with ``sdk``, or ``None`` when ``sdk`` is not
    one of ``SPACE_SDKS``."""
    if not isinstance(sdk, str) or sdk not in SPACE_SDKS:
        return None
    front = yaml.safe_dump(
        {"title": name, "emoji": "🚀", "colorFrom": "blue", "colorTo": "green", "sdk": sdk, "pinned": False},
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{front}---\n\nCheck out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference\n".encode()
