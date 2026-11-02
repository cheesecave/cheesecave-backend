#!/usr/bin/env python3
"""Seed made-up users, an organization, repositories and collections straight into the database (no LakeFS or S3).

For the collections prototype: ``CHEESE_CAVE_DB_BACKEND=sqlite CHEESE_CAVE_DATABASE_URL=sqlite:////tmp/x/hub.db
PYTHONPATH=src python scripts/dev/seed_collections_demo.py /tmp/x``. Writes ``tokens.json`` (an API token per user) into
the directory. Every name and number here is invented.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kohakuhub.auth.utils import generate_token, hash_password, hash_token
from kohakuhub.db import (
    Collection, CollectionItem, CollectionUpvote, Commit, Repository, RepositoryLike, RepositoryMetadata, Token, User,
    UserOrganization, db, init_db,
)

NOW = datetime.now(timezone.utc)


def user(name, full, org=False):
    return User.create(
        username=name, normalized_name=name, is_org=org, full_name=full, email=None if org else f"{name}@example.org",
        password_hash=None if org else hash_password("demo-pass-1"), email_verified=not org,
    )


def repo(owner, kind, name, *, private=False, downloads=0, likes=0, days=3, tags=(), pipeline=None):
    row = Repository.create(
        repo_type=kind, namespace=owner.username, name=name, full_id=f"{owner.username}/{name}", private=private,
        owner=owner, downloads=downloads, likes_count=likes, created_at=NOW - timedelta(days=days + 20),
    )
    Commit.create(repository=row, commit_id=f"{row.id:040x}", repo_type=kind, branch="main", author=owner, owner=owner,
                  username=owner.username, message="update", created_at=NOW - timedelta(days=days))
    meta = {"tags": list(tags)}
    if pipeline:
        meta["pipeline_tag"] = pipeline
    RepositoryMetadata.create(repository=row, metadata=json.dumps(meta), state="ready", checked_at=NOW, main_sha="a" * 40)
    return row


def collection(owner, title, *, private=False, description=None, theme="green", days=2, position=0):
    from kohakuhub.api.collections import new_oid, title_slug
    return Collection.create(
        oid=new_oid(), owner=owner, title=title, slug=title_slug(title), description=description, theme=theme,
        private=private, position=position, updated_at=NOW - timedelta(days=days),
    )


def put(col, *entries):
    for position, (target, note) in enumerate(entries):
        kind = "collection" if isinstance(target, Collection) else target.repo_type
        CollectionItem.create(
            oid=f"{col.id:08x}{position:016x}", collection=col, item_type=kind, note=note, position=position,
            repository=None if isinstance(target, Collection) else target, child=target if isinstance(target, Collection) else None,
        )


def main(out: Path):
    init_db()
    with db.atomic():
        alice, mika, guest = user("alice", "Alice Tanaka"), user("mika", "Mika Berg"), user("guest", "Guest Reader")
        lab = user("anime-lab", "Anime Lab", org=True)
        UserOrganization.create(user=alice, organization=lab, role="admin")
        UserOrganization.create(user=mika, organization=lab, role="member")
        UserOrganization.create(user=guest, organization=lab, role="visitor")

        t3 = repo(alice, "model", "tagger-v3", downloads=18420, likes=212, days=1, tags=["image-classification", "onnx"], pipeline="image-classification")
        t2 = repo(alice, "model", "tagger-v2", downloads=9100, likes=88, days=40, tags=["image-classification"], pipeline="image-classification")
        seg = repo(alice, "model", "segmenter", downloads=2210, likes=31, days=12, tags=["image-segmentation"], pipeline="image-segmentation")
        wip = repo(alice, "model", "wip-private", private=True, days=1, tags=["draft"])
        faces = repo(alice, "dataset", "faces-2k", downloads=5300, likes=64, days=9, tags=["faces", "cc-by-4.0"])
        sketch = repo(alice, "dataset", "sketches", downloads=720, likes=12, days=30, tags=["sketch"])
        demo = repo(alice, "space", "tagger-demo", downloads=0, likes=47, days=2, tags=["gradio"])
        ccip = repo(lab, "model", "ccip", downloads=60400, likes=530, days=5, tags=["feature-extraction", "onnx"], pipeline="feature-extraction")
        heads = repo(lab, "model", "yolo-heads", downloads=14900, likes=140, days=15, tags=["object-detection"], pipeline="object-detection")
        pix = repo(lab, "dataset", "pixiv-sample", downloads=22100, likes=301, days=7, tags=["images", "anime"])
        hdemo = repo(lab, "space", "heads-demo", likes=55, days=6, tags=["gradio"])
        emb = repo(mika, "model", "embeddings", downloads=3300, likes=40, days=4, tags=["sentence-similarity"], pipeline="sentence-similarity")
        notes = repo(mika, "dataset", "notes", downloads=90, likes=3, days=60, tags=["text"])
        RepositoryLike.create(repository=t3, user=guest)

        iccv = collection(alice, "ICCV 2026", description="Models, data and demos from our ICCV 2026 paper.", theme="orange", days=1, position=0)
        put(iccv, (t3, "The main model of the paper. Weights, ONNX export and eval scripts are in the repo."), (faces, "Training data, 2,000 faces. See https://example.org/iccv2026 for the license discussion."), (demo, None), (t2, "The previous version, kept for the ablation."))
        toolkit = collection(alice, "Anime tagging toolkit", description="Everything for tagging, from data to demo.", theme="indigo", days=3, position=1)
        reading = collection(alice, "Reading list", description="Other people's work I keep coming back to.", theme="pink", days=6, position=2)
        drafts = collection(alice, "Private drafts", private=True, description="Not public yet.", theme="blue", days=1, position=3)
        det = collection(lab, "Detection models", description="Heads, faces and full-body detectors by Anime Lab.", theme="green", days=4, position=0)
        bench = collection(lab, "Internal benchmarks", private=True, description="Members only.", theme="purple", days=2, position=1)
        mine = collection(mika, "Embedding experiments", description="A few notes on sentence embeddings.", theme="blue", days=5, position=0)

        put(det, (heads, "Fast head detector, 640 px."), (ccip, "Character identity features, used by the next collection."), (hdemo, None))
        put(bench, (ccip, None), (pix, "Held-out split lives here."))
        put(mine, (emb, None), (notes, "Raw notes, messy."))
        put(toolkit, (t3, None), (t2, None), (seg, "Experimental, masks are rough."), (faces, None), (sketch, None), (demo, None), (iccv, "The paper's own collection."))
        put(reading, (det, "Anime Lab's detectors."), (mine, None), (ccip, "Great for re-identification."))
        put(drafts, (wip, "Do not share yet."), (t3, None))

        for col, voters in ((iccv, [mika, guest]), (toolkit, [mika]), (det, [alice, guest, mika])):
            for voter in voters:
                CollectionUpvote.create(collection=col, user=voter, created_at=NOW - timedelta(days=1))
            Collection.update(upvotes_count=len(voters)).where(Collection.id == col.id).execute()

        tokens = {}
        for who in (alice, mika, guest):
            raw = generate_token()
            Token.create(user=who, token_hash=hash_token(raw), name="prototype")
            tokens[who.username] = raw
    (out / "tokens.json").write_text(json.dumps(tokens, indent=1))
    print("seeded:", {m.__name__: m.select().count() for m in (User, Repository, Collection, CollectionItem, CollectionUpvote)})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
