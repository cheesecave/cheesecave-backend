"""The Data Explorer inside the real CheeseCave web UI (Vue app on a Vite dev server, proxied to the prototype backend).

Every screen is driven by clicks, asserted against ground truth, and screenshotted for the design issue.
usage: WEB_BASE=http://127.0.0.1:5199 EXPLORER_SHOTS=/dir pytest test_web_integration.py   (needs the proxy env for the CDN)
"""
import io
import json
import os

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from playwright.sync_api import sync_playwright

WEB = os.environ.get("WEB_BASE", "http://127.0.0.1:5199")
BASE = os.environ.get("EXPLORER_BASE", "http://127.0.0.1:48999")
DATA = os.environ.get("EXPLORER_DATA", "/tmp/cc-proto/data")
SHOTS = os.environ.get("EXPLORER_SHOTS", "")
TRUTH = json.load(open(f"{DATA}/truth.json"))
TOKENS = json.load(open(f"{DATA}/tokens.json"))
http = httpx.Client(trust_env=False, timeout=120, follow_redirects=True)


@pytest.fixture(scope="module")
def browser():
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    opts = {"proxy": {"server": proxy, "bypass": "127.0.0.1,localhost"}} if proxy else {}
    if os.environ.get("EXPLORER_CHROME"):
        opts["executable_path"] = os.environ["EXPLORER_CHROME"]
    with sync_playwright() as p:
        b = p.chromium.launch(**opts)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1360, "height": 900})
    pg = ctx.new_page()
    pg.set_default_timeout(60_000)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    ctx.close()


def shot(page, name, full=False, clip_to=None):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    page.wait_for_timeout(600)           # let Element Plus enter transitions finish
    if clip_to:
        page.locator(clip_to).first.screenshot(path=f"{SHOTS}/{name}.png")
    else:
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=full)


def open_data_tab(page, repo="proto/reviews"):
    page.goto(f"{WEB}/datasets/{repo}", wait_until="domcontentloaded")
    page.click("[data-testid=tab-data]")
    page.wait_for_selector("[data-testid=explorer]")


@pytest.fixture(scope="module")
def train():
    return pa.concat_tables([pq.read_table(io.BytesIO(http.get(f"{BASE}/datasets/proto/reviews/resolve/main/data/train-0000{i}-of-00002.parquet").content)) for i in (0, 1)])


def table_rows(page, sel="[data-testid=rows]"):
    return page.eval_on_selector_all(f"{sel} tbody tr", "rows => rows.map(r => [...r.cells].map(c => c.textContent.trim()))")


def test_card_tab_shows_the_preview_and_links_to_the_data_tab(page):
    page.goto(f"{WEB}/datasets/proto/reviews", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=preview-card] tbody tr")
    assert page.locator("[data-testid=preview-card] tbody tr").count() == 4
    assert "40,000 rows" in page.text_content("[data-testid=preview-card]")
    shot(page, "w01-card-preview")
    page.click("[data-testid=viewall]")
    page.wait_for_selector("[data-testid=explorer]")
    assert page.errors == []


def test_data_tab_rows_match_ground_truth(page, train):
    open_data_tab(page)
    page.wait_for_selector("[data-testid=total]")
    assert page.text_content("[data-testid=total]").strip() == "40,000 rows"
    assert page.text_content("[data-testid=source]").strip() == "from README configs"
    rows = table_rows(page)
    assert len(rows) == 25 and [int(r[0]) for r in rows] == list(range(25))
    names = ["very negative", "negative", "neutral", "positive", "very positive"]
    want = train.slice(0, 25)
    assert [int(r[1]) for r in rows] == want["id"].to_pylist()
    assert [r[3] for r in rows] == [names[v] for v in want["label"].to_pylist()]
    page.wait_for_selector("[data-testid=stat-label]")
    assert "exact · footer" in page.text_content("[data-testid=stat-text]")
    page.wait_for_selector("[data-testid=flow] tbody tr")
    shot(page, "w02-data-tab")
    shot(page, "w02b-data-tab-full", full=True)
    assert page.errors == []


def test_paging_and_split_switch(page):
    open_data_tab(page)
    page.wait_for_selector("[data-testid=total]")
    page.click("[data-testid=next]")
    page.wait_for_function("document.querySelector('[data-testid=range]')?.textContent.includes('rows 25')")
    page.click("[data-testid=split]")
    page.click("li:has-text('test')")
    page.wait_for_function("document.querySelector('[data-testid=total]')?.textContent.trim() === '8,000 rows'")


def test_filter_and_search_and_error(page, train):
    open_data_tab(page)
    page.wait_for_selector("[data-testid=total]")
    page.click("[data-testid=mode] >> text=Filter")
    page.fill("[data-testid=term]", '"label"=4 AND "score">3.5')
    page.click("[data-testid=go]")
    page.wait_for_function("document.querySelector('[data-testid=rows] tbody tr td:nth-child(4)')?.textContent.trim() === 'very positive'")
    rows = table_rows(page)
    mask = [(l == 4 and s > 3.5) for l, s in zip(train["label"].to_pylist(), train["score"].to_pylist())]
    assert [int(r[0]) for r in rows] == [i for i, m in enumerate(mask) if m][:25]
    shot(page, "w03-filter")
    page.fill("[data-testid=term]", '"label"=1; DROP TABLE x')
    page.click("[data-testid=go]")
    page.wait_for_selector("[data-testid=error]")
    assert "InvalidFilter" in page.text_content("[data-testid=error]")
    shot(page, "w04-filter-error")


def test_images_are_thumbnails_read_by_range(page):
    open_data_tab(page, "proto/images-parquet")
    page.wait_for_selector("[data-testid=thumb]")
    page.wait_for_function("[...document.querySelectorAll('[data-testid=thumb]')].every(i => i.complete && i.naturalWidth > 0)")
    assert page.locator("[data-testid=thumb]").count() == 25
    page.wait_for_function("document.querySelectorAll('[data-testid=flow] tbody tr').length >= 6")
    assert "image column (visible rows)" in page.text_content("[data-testid=flow]")
    shot(page, "w05-media-cells")


def test_sql_group_by_and_footer_count_and_budget(page):
    open_data_tab(page)
    page.click("[data-testid=view-sql]")
    page.click("[data-testid=run]")
    page.wait_for_selector("[data-testid=exact]", timeout=120_000)
    got = table_rows(page, "[data-testid=sqltable]")
    assert [int(r[1]) for r in got] == TRUTH["reviews"]["label_hist_train"]
    shot(page, "w06-sql")
    page.fill("[data-testid=sql]", "SELECT count(*) FROM train")
    page.click("[data-testid=run]")
    page.wait_for_selector("[data-testid=footer]")
    assert table_rows(page, "[data-testid=sqltable]") == [["40000"]]
    shot(page, "w07-sql-footer-count")


def test_archive_grid(page):
    open_data_tab(page, "proto/webds")
    page.wait_for_selector("[data-testid=sample]")             # a dataset of archives opens on the grid by itself
    assert page.locator("[data-testid=error]").count() == 0     # and never asks for rows
    assert page.is_disabled("[data-testid=view-rows]") and page.is_disabled("[data-testid=view-sql]")
    assert "viewer/rows" not in page.text_content("[data-testid=flow]")
    assert page.text_content("[data-testid=nsamples]").strip() == "200 samples"
    page.wait_for_function("[...document.querySelectorAll('[data-testid=sample] img')].length === 12 && [...document.querySelectorAll('[data-testid=sample] img')].every(i => i.complete && i.naturalWidth > 0)")
    shot(page, "w08-archive")


def test_compare_revisions(page):
    t = TRUTH["versions"]
    open_data_tab(page, "proto/versions")
    page.click("[data-testid=view-compare]")
    page.wait_for_selector("[data-testid=diffgo]")
    page.wait_for_function("document.querySelector('[data-testid=old]')?.textContent.includes('v1')")
    page.click("[data-testid=diffgo]")
    page.wait_for_selector("[data-testid=added]")
    assert page.text_content("[data-testid=added] .text-xl").strip() == str(t["added"])
    assert page.text_content("[data-testid=removed] .text-xl").strip() == str(t["removed"])
    assert "+ flag" in page.text_content("[data-testid=diffresult]")
    shot(page, "w09-compare")


def test_private_dataset_needs_a_login(page):
    page.goto(f"{WEB}/datasets/proto/private-data", wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    assert page.locator("[data-testid=explorer]").count() == 0 and page.locator("[data-testid=tab-data]").count() == 0   # nothing of the dataset is shown
    assert "proto/private-data" not in page.locator("main").last.inner_text() or "not" in page.locator("main").last.inner_text().lower()
    shot(page, "w10-private-anonymous")


def test_dark_mode_and_phone(page):
    open_data_tab(page)
    page.wait_for_selector("[data-testid=total]")
    page.evaluate("document.documentElement.classList.add('dark')")
    page.wait_for_timeout(500)
    shot(page, "w11-dark")
    page.evaluate("document.documentElement.classList.remove('dark')")
    page.set_viewport_size({"width": 390, "height": 800})
    page.wait_for_timeout(600)
    assert page.evaluate("document.documentElement.scrollWidth") <= 395
    shot(page, "w12-phone")


def test_sql_over_budget_refuses_before_downloading(page):
    open_data_tab(page)
    page.click("[data-testid=view-sql]")
    page.fill("[data-testid=sql]", "SELECT 1 FROM train LIMIT 1")
    page.fill("[data-testid=budget]", "0.1")
    page.press("[data-testid=budget]", "Tab")
    page.click("[data-testid=run]")
    page.wait_for_selector("[data-testid=error]")
    assert "over the browser budget" in page.text_content("[data-testid=error]")
    assert "load train into SQL" not in page.text_content("[data-testid=flow]")      # nothing was downloaded
    shot(page, "w13-sql-over-budget")


# ---------------------------------------------------------------------------------------------------- annotated walkthrough
ANNOTATE_JS = """
(items) => {
  document.querySelectorAll('.cc-note').forEach(e => e.remove());
  for (const [selector, label, where] of items) {
    const el = document.querySelector(selector);
    if (!el) throw new Error('annotation target not found: ' + selector);
    const r = el.getBoundingClientRect();
    const b = document.createElement('div');
    b.className = 'cc-note';
    b.textContent = label;
    const x = where === 'left' ? r.left - 14 : where === 'right' ? r.right - 14 : r.left + 6;
    b.style.cssText = `position:absolute;z-index:99999;left:${x + scrollX}px;top:${r.top + scrollY - 12}px;width:26px;height:26px;border-radius:50%;background:#c2410c;color:#fff;font:700 14px/26px system-ui;text-align:center;box-shadow:0 0 0 3px #fff,0 1px 6px #0006`;
    const o = document.createElement('div');
    o.className = 'cc-note';
    o.style.cssText = `position:absolute;z-index:99998;left:${r.left + scrollX - 3}px;top:${r.top + scrollY - 3}px;width:${r.width + 6}px;height:${r.height + 6}px;border:2px dashed #c2410c;border-radius:8px;pointer-events:none`;
    document.body.append(o, b);
  }
}
"""


def annotate(page, items):
    page.evaluate(ANNOTATE_JS, items)
    page.wait_for_timeout(300)


def test_annotated_walkthrough_targets_exist_and_are_captured(page):
    """Numbered callouts on the real pages; the legend lives in the design issue. A missing target fails the test, so the legend cannot drift."""
    page.set_viewport_size({"width": 1360, "height": 1500})      # a tall viewport, not full_page: vh-based elements would stretch
    page.goto(f"{WEB}/datasets/proto/reviews", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=preview-card] tbody tr")
    annotate(page, [("[data-testid=tab-data]", "1", "left"), ("[data-testid=preview-card]", "2", "left"), ("[data-testid=viewall]", "3", "left")])
    shot(page, "a01-card-tab")

    open_data_tab(page)
    page.wait_for_selector("[data-testid=total]")
    page.wait_for_selector("[data-testid=stat-label]")
    page.wait_for_selector("[data-testid=flow] tbody tr")
    annotate(page, [
        ("[data-testid=view-rows]", "1", "left"), ("[data-testid=commit]", "2", "left"), ("[data-testid=split]", "3", "left"),
        ("[data-testid=source]", "4", "left"), ("[data-testid=mode]", "5", "left"), ("[data-testid=rows]", "6", "left"),
        ("[data-testid=cost]", "7", "left"), ("[data-testid=stats]", "8", "left"), ("[data-testid=flow]", "9", "left"),
    ])
    shot(page, "a02-data-tab")

    page.click("[data-testid=view-sql]")
    page.click("[data-testid=run]")
    page.wait_for_selector("[data-testid=exact]", timeout=120_000)
    annotate(page, [("[data-testid=sql]", "1", "left"), ("[data-testid=budget]", "2", "left"), ("[data-testid=exact]", "3", "left"), ("[data-testid=sqltable]", "4", "left"), ("[data-testid=flow]", "5", "left")])
    shot(page, "a03-sql")

    open_data_tab(page, "proto/webds")
    page.wait_for_selector("[data-testid=sample]")
    page.wait_for_function("[...document.querySelectorAll('[data-testid=sample] img')].every(i => i.complete && i.naturalWidth > 0)")
    annotate(page, [("[data-testid=view-rows]", "1", "left"), ("[data-testid=tarpick]", "2", "left"), ("[data-testid=nsamples]", "3", "left"), ("[data-testid=cost]", "4", "left"), ("[data-testid=samples]", "5", "left"), ("[data-testid=flow]", "6", "left")])
    shot(page, "a04-archive")

    page.goto(f"{WEB}/datasets/proto/versions", wait_until="domcontentloaded")
    page.click("[data-testid=tab-data]")
    page.click("[data-testid=view-compare]")
    page.wait_for_function("document.querySelector('[data-testid=old]')?.textContent.includes('v1')")
    page.click("[data-testid=diffgo]")
    page.wait_for_selector("[data-testid=examples]")
    annotate(page, [("[data-testid=old]", "1", "left"), ("[data-testid=new]", "2", "left"), ("[data-testid=key]", "3", "left"), ("[data-testid=rows]", "4", "left"), ("[data-testid=added]", "5", "left"), ("[data-testid=examples]", "6", "left")])
    shot(page, "a05-compare")
