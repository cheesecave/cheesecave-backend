"""Pure cache capacity and filesystem ownership; no database decisions are mocked."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi import HTTPException
import pytest

from kohakuhub import repository_media as service


def test_concurrent_reservations_include_inflight_builds_in_hard_capacity(tmp_path):
    cache=service.DerivativeCache(tmp_path/"cache")
    for i in range(3):
        d=cache.root/f"render-{i}";d.mkdir()
        cache.entries[str(i)]=service.Entry(d,"pdf",{},time.monotonic(),pins=1)
    start=threading.Barrier(2);finish=threading.Barrier(2)
    def reserve(i):
        start.wait()
        try:cache.reserve(str(i)*64);ok=True
        except HTTPException as error:assert error.status_code==429;ok=False
        finish.wait()
        return ok
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,[7,8]))
    assert results.count(True)==1
    assert len(cache.entries)+len(cache.building)==4
    for key in list(cache.building):cache.release(key)


def test_recursive_cache_cleanup_refuses_paths_outside_its_own_root(tmp_path):
    cache=service.DerivativeCache(tmp_path/"cache")
    outside=tmp_path/"user-data";outside.mkdir();(outside/"keep").write_text("retain")
    with pytest.raises(ValueError):cache.remove(outside)
    with pytest.raises(ValueError):cache.remove(cache.root)
    assert (outside/"keep").read_text()=="retain"



def test_cache_hit_expiry_pin_and_atomic_publication(tmp_path,monkeypatch):
    cache=service.DerivativeCache(tmp_path/"cache")
    key="a"*64;directory=cache.root/"render-one";directory.mkdir()
    cache.reserve(key)
    entry=service.Entry(directory,"pdf",{"page_count":1},time.monotonic())
    cache.publish(key,entry);cache.release(key)
    assert cache.get(key) is entry and not cache.building
    with cache.pin(key):assert entry.pins==1
    assert entry.pins==0
    entry.accessed-=service.CACHE_TTL+1
    assert cache.get(key) is None and not directory.exists()
    with pytest.raises(HTTPException) as error:
        with cache.pin(key):pass
    assert error.value.status_code==409


def test_duplicate_build_and_native_slots_are_bounded(tmp_path):
    cache=service.DerivativeCache(tmp_path/"cache")
    cache.reserve("a"*64)
    with pytest.raises(HTTPException) as error:cache.reserve("a"*64)
    assert error.value.status_code==429
    cache.reserve("b"*64)
    with pytest.raises(HTTPException):cache.reserve("c"*64)
    cache.release("a"*64);cache.release("b"*64)
    cache.reserve("c"*64);cache.release("c"*64)


def test_native_process_timeout_failure_and_output_caps_are_enforced(tmp_path,monkeypatch):
    import sys
    root=tmp_path/"render";root.mkdir()
    with pytest.raises(HTTPException) as error:service._run([sys.executable,"-c","import time;time.sleep(30)"],root,0.05)
    assert error.value.status_code==504
    with pytest.raises(HTTPException) as error:service._run([sys.executable,"-c","raise SystemExit(2)"],root,5)
    assert error.value.status_code==422
    monkeypatch.setattr(service,"OUTPUT_LIMIT",10)
    with pytest.raises(HTTPException) as error:service._run([sys.executable,"-c","from pathlib import Path;import time;Path(r'"+str(root/"large")+"').write_bytes(b'x'*20);time.sleep(3)"],root,5)
    assert error.value.status_code==413
    (root/"large").unlink()
    with pytest.raises(HTTPException) as error:service._run([sys.executable,"-c","from pathlib import Path;Path(r'"+str(root/"large")+"').write_bytes(b'x'*20)"],root,5)
    assert error.value.status_code==413
    with pytest.raises(HTTPException) as error:service._run([str(root/"missing-executable")],root,5)
    assert error.value.status_code==503


@pytest.mark.parametrize("body,status",[
    ("#EXTM3U\n#EXTINF:nan,\nsegment-0000.ts\n",422),
    ("#EXTM3U\n#EXTINF:-1,\nsegment-0000.ts\n",422),
    ("#EXTM3U\n#EXTINF:31,\nsegment-0000.ts\n",422),
    ("#EXTM3U\n#EXTINF:1,\nhttps://storage.invalid/raw\n",422),
    ("#EXTM3U\n#EXTINF:1,\nsegment-0001.ts\n",422),
    ("#EXTM3U\n#EXTINF:1,\n",422),
    ("#EXTM3U\n",422),
])
def test_encoder_output_is_validated_before_building_any_public_playlist(tmp_path,monkeypatch,body,status):
    (tmp_path/"index.m3u8").write_text(body)
    monkeypatch.setattr(service,"_run",lambda *args:None)
    with pytest.raises(HTTPException) as error:service._hls(tmp_path,"video")
    assert error.value.status_code==status


def test_duration_and_asset_size_caps_use_generated_output_not_caller_offsets(tmp_path,monkeypatch):
    (tmp_path/"index.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nsegment-0000.ts\n")
    (tmp_path/"segment-0000.ts").write_bytes(b'x'*100)
    monkeypatch.setattr(service,"_run",lambda *args:None)
    monkeypatch.setattr(service,"MAX_DURATION",1)
    with pytest.raises(HTTPException) as error:service._hls(tmp_path,"audio")
    assert error.value.status_code==413
    monkeypatch.setattr(service,"MAX_DURATION",600);monkeypatch.setattr(service,"ASSET_LIMIT",10)
    with pytest.raises(HTTPException) as error:service._hls(tmp_path,"audio")
    assert error.value.status_code==413


@pytest.mark.asyncio
async def test_cancelled_awaiter_keeps_native_slot_until_its_worker_finishes():
    import asyncio
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    def work():entered.set();release.wait(2);finished.set()
    task=asyncio.create_task(service._thread(work))
    await asyncio.to_thread(entered.wait,1)
    task.cancel();await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert finished.is_set()



def test_pdf_child_is_bound_to_own_checkout_even_without_pythonpath(tmp_path,monkeypatch):
    from test.kohakuhub.api.test_repository_guest_media_unit import pdf_bytes
    import json
    root=tmp_path/"render";root.mkdir();(root/"source.bin").write_bytes(pdf_bytes())
    monkeypatch.delenv("PYTHONPATH",raising=False);monkeypatch.chdir(tmp_path)
    service._pdf(root)
    assert json.loads((root/"metadata.json").read_text())["page_count"]==2



def test_a_child_that_exits_between_polls_still_has_its_output_budget_checked(tmp_path,monkeypatch):
    # External native-process boundary: model a completed child deterministically,
    # so this limit check does not depend on process scheduling or disk speed.
    class CompletedProcess:
        returncode=0
        def poll(self):return 0
        def wait(self):return 0
    (tmp_path/"completed-output").write_bytes(b"x"*20)
    monkeypatch.setattr(service.subprocess,"Popen",lambda *args,**kwargs:CompletedProcess())
    monkeypatch.setattr(service,"OUTPUT_LIMIT",10)
    with pytest.raises(HTTPException) as error:service._run(["completed-native-worker"],tmp_path,1)
    assert error.value.status_code==413
