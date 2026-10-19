"""API tests for admin statistics routes."""


async def test_admin_stats_overview_and_detailed_payloads(admin_client):
    overview_response = await admin_client.get("/admin/api/stats")
    assert overview_response.status_code == 200
    overview = overview_response.json()
    assert overview["users"] >= 4
    assert overview["organizations"] >= 1
    assert overview["repositories"]["total"] >= 2
    assert overview["repositories"]["public"] >= 1

    detailed_response = await admin_client.get("/admin/api/stats/detailed")
    assert detailed_response.status_code == 200
    detailed = detailed_response.json()
    assert detailed["users"]["total"] >= detailed["users"]["active"]
    assert detailed["users"]["inactive"] == (
        detailed["users"]["total"] - detailed["users"]["active"]
    )
    assert detailed["repositories"]["by_type"]["model"] >= 1
    assert detailed["repositories"]["by_type"]["dataset"] >= 1
    assert detailed["commits"]["total"] >= 1
    assert isinstance(detailed["commits"]["top_contributors"], list)


async def test_admin_stats_timeseries_and_top_repo_rankings(admin_client):
    timeseries_response = await admin_client.get("/admin/api/stats/timeseries", params={"days": 30})
    assert timeseries_response.status_code == 200
    timeseries = timeseries_response.json()
    assert "repositories_by_day" in timeseries
    assert "commits_by_day" in timeseries
    assert "users_by_day" in timeseries
    assert timeseries["repositories_by_day"]
    assert timeseries["commits_by_day"]
    assert timeseries["users_by_day"]

    top_commits_response = await admin_client.get(
        "/admin/api/stats/top-repos",
        params={"by": "commits", "limit": 5},
    )
    assert top_commits_response.status_code == 200
    top_commits = top_commits_response.json()
    assert top_commits["sorted_by"] == "commits"
    assert top_commits["top_repositories"]
    assert "commit_count" in top_commits["top_repositories"][0]

    top_size_response = await admin_client.get(
        "/admin/api/stats/top-repos",
        params={"by": "size", "limit": 5},
    )
    assert top_size_response.status_code == 200
    top_size = top_size_response.json()
    assert top_size["sorted_by"] == "size"
    assert top_size["top_repositories"]
    assert "total_size" in top_size["top_repositories"][0]


# The Overview was added beside these endpoints: their shapes are frozen (an admin build already in a browser still calls them).
async def test_the_old_stats_endpoints_keep_their_exact_shapes(admin_client):
    stats = (await admin_client.get("/admin/api/stats")).json()
    assert set(stats) == {"users", "organizations", "repositories"}
    assert set(stats["repositories"]) == {"total", "private", "public"}

    detailed = (await admin_client.get("/admin/api/stats/detailed")).json()
    assert set(detailed) == {"users", "organizations", "repositories", "commits", "lfs", "storage"}
    assert set(detailed["users"]) == {"total", "active", "verified", "inactive"}
    assert set(detailed["repositories"]) == {"total", "private", "public", "by_type"}
    assert set(detailed["repositories"]["by_type"]) == {"model", "dataset", "space"}
    assert set(detailed["commits"]) == {"total", "top_contributors"}
    assert set(detailed["lfs"]) == {"total_objects", "total_size"}
    assert set(detailed["storage"]) == {"private_used", "public_used", "total_used"}

    series = (await admin_client.get("/admin/api/stats/timeseries", params={"days": 7})).json()
    assert set(series) == {"repositories_by_day", "commits_by_day", "users_by_day"}

    by_commits = (await admin_client.get("/admin/api/stats/top-repos", params={"by": "commits"})).json()
    assert set(by_commits) == {"top_repositories", "sorted_by"} and by_commits["sorted_by"] == "commits"
    assert set(by_commits["top_repositories"][0]) == {"repo_full_id", "repo_type", "commit_count", "private"}
    by_size = (await admin_client.get("/admin/api/stats/top-repos", params={"by": "size"})).json()
    assert by_size["sorted_by"] == "size"
    for row in by_size["top_repositories"]:
        assert set(row) == {"repo_full_id", "repo_type", "total_size", "private"}
