#!/usr/bin/env python3
"""Seed made-up rows for the admin database viewer prototype (tasks, workers, LFS, downloads, uploads, ...).

usage: CHEESE_CAVE_DB_BACKEND=postgres CHEESE_CAVE_DATABASE_URL=postgresql://... PYTHONPATH=src \\
       python scripts/dev/seed_db_panel_demo.py
Every name and number is invented. Run it once on an empty database; it ends with ANALYZE so the estimates exist.
"""
import json
import random
from datetime import datetime, timedelta, timezone

from kohakuhub.auth.utils import hash_password
from kohakuhub.db import (
    AdminAuthClient, AdminSession, Commit, File, BackgroundTask, BackgroundWorker, Collection, CollectionItem, DailyRepoStats,
    FallbackSource, Invitation, LFSObjectHistory, Repository, StagingUpload, User, UserOrganization, db, init_db,
)

NOW = datetime.now(timezone.utc)
rnd = random.Random(7)


def ago(**kw):
    return NOW - timedelta(**kw)


def main():
    init_db()
    people = [
        User.create(username=n, normalized_name=n, email=f"{n}@example.org", password_hash=hash_password("demo-pass-1"),
                    email_verified=v, created_at=ago(days=d), private_used_bytes=u, private_quota_bytes=q)
        for n, v, d, u, q in [
            ("alice", True, 40, 12 * 2**30, 10 * 2**30), ("bob", True, 12, 2**30, 10 * 2**30), ("carol", False, 5, 0, None),
            ("dave", True, 2, 5 * 2**20, 10 * 2**30), ("erin", False, 1, 0, None), ("frank", True, 90, 3 * 2**30, None),
        ]
    ]
    org = User.create(username="acme-labs", normalized_name="acme-labs", is_org=True, created_at=ago(days=60))
    for u in people[:4]:
        UserOrganization.create(user=u, organization=org, role="member")
    repos = []
    for i, (owner, kind, name, size) in enumerate([
        (people[0], "model", "vision-base", 40 * 2**30), (people[0], "dataset", "cats-and-dogs", 9 * 2**30),
        (people[1], "model", "tiny-llm", 3 * 2**30), (org, "model", "speech-small", 6 * 2**30),
        (org, "dataset", "street-signs", 14 * 2**30), (people[5], "space", "demo-app", 2**20), (people[3], "model", "ocr-lite", 700 * 2**20),
    ]):
        repos.append(Repository.create(
            repo_type=kind, namespace=owner.username, name=name, full_id=f"{owner.username}/{name}", private=i % 3 == 2,
            owner=owner, downloads=rnd.randint(10, 9000), likes_count=rnd.randint(0, 120), used_bytes=size,
            created_at=ago(days=30 - i),
        ))
        for k in range(rnd.randint(3, 9)):
            LFSObjectHistory.create(repository=repos[-1], path_in_repo=f"weights/part-{k}.safetensors", sha256=f"{rnd.getrandbits(256):064x}",
                                    size=rnd.randint(2**20, 6 * 2**30), commit_id=f"{rnd.getrandbits(160):040x}")
        for d in range(10):
            DailyRepoStats.create(repository=repos[-1], date=(NOW - timedelta(days=d)).date(),
                                  download_sessions=(s := rnd.randint(0, 300)), authenticated_downloads=s // 3, anonymous_downloads=s - s // 3)
    for i, r in enumerate(repos):
        for k in range(rnd.randint(2, 6)):
            Commit.create(repository=r, commit_id=f"{rnd.getrandbits(160):040x}", repo_type=r.repo_type, branch="main", author=people[i % 4],
                          owner=r.owner, username=people[i % 4].username, message="update", created_at=ago(days=rnd.randint(0, 20)))
        for k, ext in enumerate(["safetensors", "json", "md", "bin", "txt", "png", "parquet"]):
            File.create(repository=r, path_in_repo=f"f{k}.{ext}", size=rnd.randint(10**3, 10**9), sha256=f"{rnd.getrandbits(256):064x}",
                        lfs=ext in ("safetensors", "bin", "parquet"), owner=r.owner)
    for i in range(3):
        StagingUpload.create(repository=repos[i], repo_type="model", revision="main", path_in_repo=f"upload-{i}.bin", size=2**28,
                             storage_key=f"staging/{i}", uploader=people[i], created_at=ago(hours=30 + 50 * i))
    StagingUpload.create(repository=repos[3], repo_type="model", revision="main", path_in_repo="fresh.bin", size=2**20, storage_key="staging/9",
                         uploader=people[3], created_at=ago(minutes=20))
    for status, kind, n in [("succeeded", "lfs.gc", 120), ("succeeded", "repo.index", 300), ("queued", "repo.index", 4), ("running", "lfs.gc", 1),
                            ("failed", "repo.index", 3), ("cancelled", "repo.export", 2)]:
        for k in range(n):
            BackgroundTask.create(kind=kind, status=status, attempts=5 if status == "failed" else 1, created_at=ago(hours=rnd.randint(1, 200)),
                                  started_at=ago(hours=1) if status != "queued" else None,
                                  locked_until=ago(minutes=40) if status == "running" else None,
                                  last_error="ConnectionError: storage endpoint did not answer in 30 s" if status == "failed" else None)
    for name, state, silent in [("worker-a", "running", 5), ("worker-b", "running", 12), ("worker-c", "stopped", 4000)]:
        BackgroundWorker.create(id=name, name=name, hostname=name, pid=100, queues="[]", concurrency=4, state=state, succeeded=rnd.randint(50, 900),
                                failed=rnd.randint(0, 9), started_at=ago(days=2), last_heartbeat_at=ago(seconds=silent))
    Invitation.create(token="demo-1", action="join_org", created_by=people[0], expires_at=NOW + timedelta(days=3), max_usage=None, parameters="{}")
    Invitation.create(token="demo-2", action="register_account", created_by=people[0], expires_at=NOW + timedelta(days=30), max_usage=-1, usage_count=7, parameters="{}", used_at=ago(days=3))
    FallbackSource.create(namespace="", url="https://huggingface.co", token="hf_demo_not_a_token", priority=10, name="HuggingFace", source_type="huggingface")
    FallbackSource.create(namespace="acme-labs", url="https://hub.example.org", priority=20, name="Partner hub", source_type="kohakuhub", enabled=False)
    AdminSession.create(token_hash="a" * 64, principal_type="token", created_at=ago(hours=2), last_used_at=ago(minutes=3), expires_at=NOW + timedelta(hours=6),
                        absolute_expires_at=NOW + timedelta(hours=22), ip="203.0.113.7", user_agent="Mozilla/5.0")
    AdminSession.create(token_hash="b" * 64, principal_type="token", created_at=ago(days=2), last_used_at=ago(days=1), expires_at=ago(hours=20),
                        absolute_expires_at=ago(hours=1), ip="198.51.100.23", revoked_at=ago(days=1))
    AdminAuthClient.create(client_key="198.51.100.99", failures=4, last_failure=NOW.timestamp() - 600, wait_until=NOW.timestamp() + 120)
    from kohakuhub.api.collections import new_oid, title_slug
    for i, (owner, title, up) in enumerate([(people[0], "Vision starter pack", 14), (org, "Speech models", 6), (people[1], "Small LLMs", 21)]):
        col = Collection.create(oid=new_oid(), owner=owner, title=title, slug=title_slug(title), theme="green", private=False, position=i, upvotes_count=up)
        for p, r in enumerate(repos[i:i + 2]):
            CollectionItem.create(oid=f"{col.id:08x}{p:016x}", collection=col, item_type=r.repo_type, position=p, repository=r)
    for table in db.get_tables():
        db.execute_sql(f'ANALYZE "{table}"')
    print("seeded")


if __name__ == "__main__":
    main()
