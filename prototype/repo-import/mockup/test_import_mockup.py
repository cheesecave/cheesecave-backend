"""MOCKUP of the repository import screens inside the real CheeseCave web app (Vue, Vite dev server).

The import API does not exist yet: every answer here is faked with Playwright routes, the data is synthetic, and the point is the design of the
screens and of the API shapes they need. The tests check what each state shows and that every numbered callout points at a real element.
usage: WEB_BASE=http://127.0.0.1:5199 MOCK_SHOTS=/dir pytest test_import_mockup.py   (needs the proxy env for fonts/CDN only)
"""
import json
import os
import re

import pytest
from playwright.sync_api import sync_playwright

WEB = os.environ.get("WEB_BASE", "http://127.0.0.1:5199")
SHOTS = os.environ.get("MOCK_SHOTS", "")
SHA = "9f2c61d0a4b87e3351c0de48a7b6f1209c3d5e8a"

FILES = [
    ("config.json", 0.0006e9, False, False), ("tokenizer.json", 0.0021e9, False, False), ("README.md", 0.0000e9 + 14000, False, False),
    ("model-00001-of-00003.safetensors", 4.97e9, True, False), ("model-00002-of-00003.safetensors", 4.99e9, True, False),
    ("model-00003-of-00003.safetensors", 4.54e9, True, True), ("tokenizer.model", 0.5e6, True, True),
]


def preview(over=False):
    files = [{"path": p, "size": int(s), "lfs": lfs, "present": present} for p, s, lfs, present in FILES]
    new = sum(f["size"] for f in files if not f["present"])
    dedup = sum(f["size"] for f in files if f["present"])
    return {"source_sha": SHA, "warnings": ["license: apache-2.0 (kept in README)", "not gated"], "files": files,
            "totals": {"files": 143, "bytes": 26_400_000_000 + dedup, "new_bytes": 26_400_000_000 if over else new + 11_800_000_000, "deduped_bytes": dedup},
            "quota": {"used": 41_000_000_000, "limit": 50_000_000_000 if over else 200_000_000_000}}


def job(state):
    base = {"id": 41, "target": "narugo1992/example-model-7b", "source": "HuggingFace", "source_repo": "example-org/example-model-7b", "source_sha": SHA,
            "bytes_total": 14_500_000_000, "files_total": 6, "skipped_files": 2, "skipped_bytes": 4_540_000_000, "runner": "runner-eu-1", "notice": None,
            "error": None, "report": None, "log": ["listing example-org/example-model-7b@9f2c61d0", "6 files selected, 2 already stored, 4 to transfer",
                                                  "model-00001-of-00003.safetensors: part 10 of 10 uploaded"]}
    done_f, sk = "done", "skipped"
    if state == "running":
        return {**base, "status": "running", "stage": "Transferring", "bytes_done": 7_900_000_000, "files_done": 3, "rate": 38, "eta": "3 min", "verified": 3,
                "files": [{"path": "config.json", "size": 600, "state": done_f}, {"path": "model-00001-of-00003.safetensors", "size": 4_970_000_000, "state": done_f},
                          {"path": "model-00002-of-00003.safetensors", "size": 4_990_000_000, "state": "transferring", "done": 3_100_000_000},
                          {"path": "model-00003-of-00003.safetensors", "size": 4_540_000_000, "state": sk}, {"path": "tokenizer.model", "size": 500_000, "state": sk},
                          {"path": "README.md", "size": 14000, "state": "queued"}]}
    if state == "rate_limited":
        r = job("running")
        return {**r, "rate": 0, "eta": "", "notice": "The source answered 429 (rate limit). Paused, retrying in 42 s; the limit window resets at 14:05:12."}
    if state == "done":
        return {**base, "status": "succeeded", "stage": "Done", "bytes_done": 14_500_000_000, "files_done": 6, "rate": 0, "eta": "", "verified": 6,
                "report": "Verified 6 of 6 files by sha256 (4 transferred, 2 already stored). One commit, 3e91c7b2, records source example-org/example-model-7b@9f2c61d0.",
                "files": [{"path": p, "size": int(s), "state": sk if present else done_f} for p, s, _, present in FILES[:6]]}
    return {**base, "id": 43, "status": "failed", "stage": "Listing", "bytes_done": 0, "files_done": 0, "files_total": 0, "rate": 0, "eta": "", "verified": 0,
            "files": [], "runner": None, "error": {"code": "SourceUnauthorized", "message": "The source answered 401: this repository is gated. Add your Hugging Face token in Settings > External sources, accept the licence on the source, and retry."}}


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
    ctx = browser.new_context(viewport={"width": 1360, "height": 1000})
    pg = ctx.new_page()
    pg.set_default_timeout(45_000)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))

    def api(route):
        url, method = route.request.url, route.request.method
        if url.endswith("/api/repos/import/preview") and method == "POST":
            body = json.loads(route.request.post_data or "{}")
            return route.fulfill(json=preview(over="huge" in body.get("repo", "")))
        if "/api/repos/import/" in url and method == "GET":
            return route.fulfill(json=job(pg.state))
        return route.continue_()

    pg.state = "running"
    pg.route("**/api/repos/import/**", api)
    yield pg
    ctx.close()


NOTE_JS = """
(items) => {
  document.querySelectorAll('.cc-note').forEach(e => e.remove());
  for (const [selector, label] of items) {
    const el = document.querySelector(selector);
    if (!el) throw new Error('annotation target not found: ' + selector);
    const r = el.getBoundingClientRect();
    const b = document.createElement('div'); b.className = 'cc-note'; b.textContent = label;
    b.style.cssText = `position:absolute;z-index:99999;left:${Math.max(4, r.left + scrollX - 34)}px;top:${r.top + scrollY + Math.min(r.height / 2, 24) - 13}px;width:26px;height:26px;border-radius:50%;background:#c2410c;color:#fff;font:700 14px/26px system-ui;text-align:center;box-shadow:0 0 0 3px #fff,0 1px 6px #0006`;
    const o = document.createElement('div'); o.className = 'cc-note';
    o.style.cssText = `position:absolute;z-index:99998;left:${r.left + scrollX - 3}px;top:${r.top + scrollY - 3}px;width:${r.width + 6}px;height:${r.height + 6}px;border:2px dashed #c2410c;border-radius:8px;pointer-events:none`;
    document.body.append(o, b);
  }
}
"""


def shot(page, name, notes=None):
    if notes:
        page.evaluate(NOTE_JS, notes)
    page.wait_for_timeout(700)
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=f"{SHOTS}/{name}.png")


def test_the_new_repository_page_points_at_import(page):
    page.goto(f"{WEB}/new", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=import-link]")
    shot(page, "m01-new-page-link", [("[data-testid=import-link]", "1")])
    page.click("[data-testid=import-link]")
    page.wait_for_selector("[data-testid=form]")


def test_a_repository_served_from_another_hub_offers_import_to_local(page):
    def patched(route):
        resp = route.fetch()
        body = resp.json()
        body.update({"_source": "HuggingFace", "_source_url": "https://huggingface.co/datasets/proto/reviews"})
        route.fulfill(response=resp, json=body)

    page.route(re.compile(r".*/api/datasets/proto/reviews(\?.*)?$"), patched)
    page.goto(f"{WEB}/datasets/proto/reviews", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=import-to-local]")
    shot(page, "m02-import-to-local-button", [("[data-testid=import-to-local]", "1")])
    page.click("[data-testid=import-to-local]")
    page.wait_for_selector("[data-testid=preview]")
    assert page.input_value("[data-testid=repo] input") == "proto/reviews" if page.locator("[data-testid=repo] input").count() else True


def test_preview_shows_what_is_new_what_is_already_stored_and_the_quota(page):
    page.goto(f"{WEB}/import?type=model&repo=example-org/example-model-7b&preview=1", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=preview]")
    assert page.text_content("[data-testid=t-files]").strip() == "143"
    assert "skipped (already stored)" in page.text_content("[data-testid=preview-files]")
    assert page.locator("[data-testid=over-quota]").count() == 0 and page.is_enabled("[data-testid=start-btn]")
    shot(page, "m03-form-and-preview", [("[data-testid=source]", "1"), ("[data-testid=allow]", "2"), ("[data-testid=preview-btn]", "3"), ("[data-testid=t-saved]", "4"),
                                         ("[data-testid=t-new]", "5"), ("[data-testid=quota]", "6"), ("[data-testid=preview-files]", "7"), ("[data-testid=start-btn]", "8")])
    assert page.errors == []


def test_an_import_over_quota_cannot_be_started(page):
    page.goto(f"{WEB}/import?type=model&repo=example-org/huge-model&preview=1", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=over-quota]")
    assert page.is_disabled("[data-testid=start-btn]")
    shot(page, "m04-over-quota", [("[data-testid=quota]", "1"), ("[data-testid=over-quota]", "2"), ("[data-testid=start-btn]", "3")])


def test_progress_while_running(page):
    page.state = "running"
    page.goto(f"{WEB}/import?id=41", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=progress]")
    assert page.text_content("[data-testid=status]").strip() == "running"
    assert "runner-eu-1" in page.text_content("[data-testid=runner]")
    assert "already stored" in page.text_content("[data-testid=files]")
    shot(page, "m05-progress-running", [("[data-testid=status]", "1"), ("[data-testid=steps]", "2"), ("[data-testid=bytes]", "3"), ("[data-testid=runner]", "4"),
                                         ("[data-testid=integrity]", "5"), ("[data-testid=files]", "6"), ("[data-testid=cancel]", "7")])


def test_progress_when_the_source_rate_limits(page):
    page.state = "rate_limited"
    page.goto(f"{WEB}/import?id=41", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=job-notice]")
    assert "429" in page.text_content("[data-testid=job-notice]")
    shot(page, "m06-rate-limited", [("[data-testid=job-notice]", "1")])


def test_done_shows_a_verified_report(page):
    page.state = "done"
    page.goto(f"{WEB}/import?id=41", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=report]")
    assert "6 of 6 sha256 verified" in page.text_content("[data-testid=integrity]")
    assert page.locator("[data-testid=cancel]").count() == 0 and page.locator("[data-testid=open]").count() == 1
    shot(page, "m07-done", [("[data-testid=report]", "1"), ("[data-testid=open]", "2")])


def test_a_gated_source_fails_early_with_what_to_do(page):
    page.state = "failed"
    page.goto(f"{WEB}/import?id=43", wait_until="domcontentloaded")
    page.wait_for_selector("[data-testid=job-error]")
    assert "SourceUnauthorized" in page.text_content("[data-testid=job-error]") and page.locator("[data-testid=retry]").count() == 1
    shot(page, "m08-failed-gated", [("[data-testid=job-error]", "1"), ("[data-testid=retry]", "2")])
