"""GET /admin/api/overview on the real app: shape, sections, range, isolation, caching, auth."""

from __future__ import annotations

import sys

import pytest

URL = "/admin/api/overview"


@pytest.fixture(autouse=True)
def fresh_cache():
    """The app (reloaded by the bootstrap) keeps a module-level cache; one test must not see another's values."""
    module = sys.modules["kohakuhub.admin_overview"]
    module.cache.clear()
    yield module
    module.cache.clear()


async def test_without_a_token_the_overview_is_refused(client):
    assert (await client.get(URL)).status_code == 401


async def test_every_section_is_returned_by_default_with_its_own_freshness(admin_client, fresh_cache):
    response = await admin_client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["schema"] == 1 and body["range"] == "7d" and body["generated_at"].endswith("Z")
    assert set(body["sections"]) == set(fresh_cache.SECTIONS)
    for name, section in body["sections"].items():
        assert "error" not in section, (name, section)
        assert section["generated_at"].endswith("Z") and section["cache"] == {"hit": False, "age_s": 0}
    s = body["sections"]
    assert s["health"]["status"] in {"ok", "degraded", "disabled"}
    assert {d["name"] for d in s["health"]["dependencies"]} >= {"postgres"}
    assert all(set(d) == {"name", "status", "latency_ms", "detail"} for d in s["health"]["dependencies"])
    assert set(s["workers"]) >= {"online", "draining", "lost"}
    assert len(s["queue"]["hourly"]) == 24
    assert isinstance(s["build"]["backend"], str)
    assert isinstance(s["attention"]["items"], list)
    assert len(s["counts"]["users"]["series"]) == 7 and len(s["activity"]["labels"]) == 7
    assert s["activity"]["bucket"] == "day"
    assert isinstance(s["recent"]["items"], list) and len(s["recent"]["items"]) <= 10
    assert set(s["top"]) >= {"by_commits", "by_size"}


async def test_the_totals_are_the_numbers_the_old_stats_endpoint_gives(admin_client):
    detailed = (await admin_client.get("/admin/api/stats/detailed")).json()
    counts = (await admin_client.get(URL, params={"sections": "counts"})).json()["sections"]["counts"]
    assert counts["users"]["total"] == detailed["users"]["total"]
    assert counts["orgs"] == detailed["organizations"]["total"]
    assert counts["repos"]["total"] == detailed["repositories"]["total"]
    assert counts["repos"]["private"] == detailed["repositories"]["private"]
    assert counts["commits"]["total"] == detailed["commits"]["total"]
    assert counts["storage"] == {
        "total": detailed["storage"]["total_used"],
        "private": detailed["storage"]["private_used"],
        "public": detailed["storage"]["public_used"],
    }
    assert counts["lfs"] == {"objects": detailed["lfs"]["total_objects"], "size": detailed["lfs"]["total_size"]}


async def test_only_the_asked_sections_are_returned_and_unknown_names_are_ignored(admin_client):
    body = (await admin_client.get(URL, params={"sections": "build, counts ,nope"})).json()
    assert list(body["sections"]) == ["build", "counts"]
    assert (await admin_client.get(URL, params={"sections": ""})).json()["sections"] == {}


async def test_the_range_picks_the_bucket_and_a_bad_one_is_rejected(admin_client):
    hourly = (await admin_client.get(URL, params={"range": "24h", "sections": "activity,counts"})).json()
    assert hourly["range"] == "24h"
    assert hourly["sections"]["activity"]["bucket"] == "hour" and len(hourly["sections"]["activity"]["commits"]) == 24
    month = (await admin_client.get(URL, params={"range": "30d", "sections": "counts"})).json()
    assert len(month["sections"]["counts"]["commits"]["series"]) == 30
    assert (await admin_client.get(URL, params={"range": "1y"})).status_code == 422


async def test_a_second_request_is_served_from_the_cache(admin_client):
    await admin_client.get(URL, params={"sections": "counts,health"})
    again = (await admin_client.get(URL, params={"sections": "counts,health"})).json()["sections"]
    assert again["counts"]["cache"]["hit"] is True and again["health"]["cache"]["hit"] is True


async def test_one_failing_section_does_not_take_the_others_down(admin_client, fresh_cache, monkeypatch):
    def broken():
        raise RuntimeError("recent exploded")

    monkeypatch.setattr(fresh_cache, "recent", broken)
    response = await admin_client.get(URL, params={"sections": "recent,counts,build"})
    assert response.status_code == 200
    s = response.json()["sections"]
    assert s["recent"]["error"] == {"code": "section_failed", "message": "recent exploded"}
    assert "error" not in s["counts"] and s["build"]["backend"]
