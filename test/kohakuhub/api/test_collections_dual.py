"""The collections tests that depend on what the database decides, on SQLite and on PostgreSQL (#134).

Ordering, keyset paging, filters, counts, uniqueness and the races of two requests behave by the engine's SQL, so these tests
of ``test_collections.py`` run again here on a database of each engine (``db_dual``; the PostgreSQL case runs where
``CHEESE_CAVE_DATABASE_URL`` names one). The rest of that module, which only checks what the code decides, stays on the one
fast database. A test imported below is collected here and gets this module's fixtures.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from peewee import IntegrityError

from kohakuhub.api import collections as collections_api
from kohakuhub.db import Collection, CollectionItem

# fixtures and tests of the module they come from, run here on the database of each engine
from test.kohakuhub.api.test_collections import (  # noqa: F401
    library,
    test_a_collection_cannot_hold_itself_or_the_same_item_twice,
    test_a_big_limit_is_allowed_for_one_owner_without_the_items,
    test_a_deleted_repository_leaves_its_collections,
    test_a_private_collection_of_an_organization_is_listed_for_its_members_and_visitors,
    test_a_renamed_repository_stays_in_its_collections,
    test_a_second_upvote_of_the_same_moment_is_still_one_upvote,
    test_an_exact_page_has_no_link_and_a_forged_cursor_is_refused,
    test_an_owner_can_have_only_so_many_collections,
    test_batch_updates_are_all_or_nothing,
    test_delete_a_collection_takes_its_items_and_upvotes_along,
    test_filter_by_owner_item_and_text,
    test_items_are_filtered_for_the_viewer,
    test_moving_a_collection_in_the_list_of_its_owner,
    test_paging_by_the_link_header_visits_every_collection_once,
    test_paging_survives_a_change_between_pages,
    test_positions_count_up_per_owner,
    test_positions_have_gaps_after_a_delete_and_new_items_go_after_the_highest,
    test_several_items_in_the_filter_match_a_collection_with_any_of_them,
    test_sorting_by_upvotes_last_change_and_the_owners_own_order,
    test_the_default_is_trending_then_the_latest_change,
    test_the_item_filter_does_not_reveal_a_private_repository,
    test_the_same_item_added_a_moment_ago_by_another_request_is_409,
    test_the_same_title_created_a_moment_ago_by_someone_else_is_still_409,
    test_the_same_title_slug_is_409_with_the_slug_of_the_existing_one,
    test_update_an_item_note_position_and_clear_the_note,
    test_upvote_counts_once_and_can_be_taken_back,
    test_visibility_follows_the_viewer,
)
from test.kohakuhub.api.test_collections import build_world


@pytest.fixture
def api(db_dual, monkeypatch):
    monkeypatch.setattr(collections_api, "db", db_dual)     # the routes' transactions on the database of the test
    app = FastAPI()
    app.include_router(collections_api.router, prefix="/api")
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def world(db_dual):
    return build_world()


def test_an_item_is_a_repository_or_a_collection_never_both_or_neither(db_dual, world):
    col = Collection.create(oid="a" * 24, owner=world.alice, title="Check", slug="check", theme="green")
    other = Collection.create(oid="b" * 24, owner=world.alice, title="Other", slug="other", theme="green")
    for values in ({}, {"repository": world.model, "child": other}):
        with pytest.raises(IntegrityError):
            CollectionItem.create(oid="c" * 24, collection=col, item_type="model", position=0, **values)
