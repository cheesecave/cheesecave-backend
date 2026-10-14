"""`update_repo_settings(gated=...)`: what the Hub accepts, and no pretence of a protection that is not there (#61).

The Hub takes `False`, `"auto"` and `"manual"` (and the client passes `True` through). Gated repositories are not
supported, so turning gating on must be refused (501) instead of answering 200 over a public repository.
Real database; the live server and `huggingface_hub` for the client side.
"""

from __future__ import annotations

import asyncio

import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

B = "/api/models/owner/demo-model"


@pytest.mark.parametrize("value", [False, None])
async def test_turning_gating_off_succeeds_and_changes_nothing(owner_client, value):
    response = await owner_client.put(f"{B}/settings", json={"gated": value})
    assert response.status_code == 200
    assert (await owner_client.get(B)).json()["gated"] is False


@pytest.mark.parametrize("value", [True, "auto", "manual"])
async def test_turning_gating_on_is_refused_not_ignored(owner_client, value):
    response = await owner_client.put(f"{B}/settings", json={"gated": value})
    assert response.status_code == 501
    assert response.headers["x-error-code"] == "NotImplemented"
    assert "gated access is not supported" in response.headers["x-error-message"]
    assert (await owner_client.get(B)).json()["gated"] is False


async def test_an_unknown_value_is_a_bad_request(owner_client):
    response = await owner_client.put(f"{B}/settings", json={"gated": "sometimes"})
    assert response.status_code == 400
    assert "sometimes" in response.headers["x-error-message"]


async def test_a_refused_gating_applies_none_of_the_other_fields(owner_client):
    before = (await owner_client.get(B)).json()["private"]
    response = await owner_client.put(f"{B}/settings", json={"gated": "manual", "private": not before})
    assert response.status_code == 501
    assert (await owner_client.get(B)).json()["private"] is before


async def test_the_other_fields_still_apply_with_gating_off(owner_client):
    response = await owner_client.put(f"{B}/settings", json={"gated": False, "private": True})
    assert response.status_code == 200
    assert (await owner_client.get(B)).json()["private"] is True


async def test_gating_needs_the_permission_to_change_settings(member_client):
    response = await member_client.put(f"{B}/settings", json={"gated": "manual"})
    assert response.status_code in (403, 404)  # the permission check comes before the refusal


@pytest.mark.hf_client
@pytest.mark.parametrize("value", ["auto", "manual"])
async def test_update_repo_settings_of_huggingface_hub_raises_on_gating(live_server_url, hf_api_token, value):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(HfHubHTTPError) as caught:
        await asyncio.to_thread(api.update_repo_settings, "owner/demo-model", gated=value)
    assert caught.value.response.status_code == 501
    assert "gated access is not supported" in str(caught.value)  # the user reads why
    info = await asyncio.to_thread(api.model_info, "owner/demo-model")
    assert not info.gated


@pytest.mark.hf_client
async def test_update_repo_settings_of_huggingface_hub_with_gating_off_is_accepted(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    await asyncio.to_thread(api.update_repo_settings, "owner/demo-model", gated=False)
    info = await asyncio.to_thread(api.model_info, "owner/demo-model")
    assert not info.gated
