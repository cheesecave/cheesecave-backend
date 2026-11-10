"""Actual PDF/media derivatives with real guest SQL identities and scoped storage."""

import io
import hashlib
from urllib.parse import urljoin

import imageio_ffmpeg
import subprocess
from datetime import timedelta

import pytest
from PIL import Image

from kohakuhub import repository_invites as invites
from kohakuhub.api import repository_guest_media as media
from kohakuhub.api import repository_guest_preview as preview
from kohakuhub.api.repository_access import guest_cookie_name
from kohakuhub.db import RepositoryGuestSession, utcnow
from test.kohakuhub.api.test_repository_guest_preview_unit import browser, HEAD
from test.kohakuhub.support.factories import make_file


def pdf_bytes():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    stream=b"BT /F1 18 Tf 20 50 Td (Rendered private PDF) Tj ET"
    objects.append(b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream")
    out=bytearray(b"%PDF-1.4\n"); offsets=[0]
    for n,obj in enumerate(objects,1):
        offsets.append(len(out));out.extend(str(n).encode()+b" 0 obj\n"+obj+b"\nendobj\n")
    xref=len(out);out.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets[1:]:out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(b"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n"+str(xref).encode()+b"\n%%EOF\n")
    return bytes(out)


@pytest.fixture
def media_browser(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(media.service, "CACHE", media.service.DerivativeCache(tmp_path / "private-media"))
    browser.client.app.include_router(media.router, prefix="/api")
    browser.contents["document.pdf"]=pdf_bytes()
    return browser


def descriptor(b,path="document.pdf"):
    return b.client.get(b.base+"/preview",params={"path":path})


def page(b,snapshot,page=1,**params):
    return b.client.get(b.base+"/media/pdf/page",params={"path":"document.pdf","page":page,"snapshot":snapshot,**params})


def test_actual_pdf_descriptor_and_rendered_page_never_return_pdf_bytes(media_browser):
    b=media_browser; r=descriptor(b)
    assert r.status_code==200
    assert r.json()["kind"]=="pdf" and r.json()["page_count"]==2
    assert set(r.json())=={"kind","page_count","snapshot"}
    image=page(b,r.json()["snapshot"])
    assert image.status_code==200 and image.headers["content-type"]=="image/png"
    assert image.content.startswith(b"\x89PNG") and not image.content.startswith(b"%PDF")
    with Image.open(io.BytesIO(image.content)) as im:
        assert max(im.size)<=1600 and im.size[0]>im.size[1]
    assert image.headers["cache-control"]=="no-store" and "location" not in image.headers


def test_media_assets_recheck_revocation_even_when_derivative_is_cached(media_browser):
    b=media_browser; payload=descriptor(b).json(); assert page(b,payload["snapshot"]).status_code==200
    RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id==b.guest.id).execute()
    assert page(b,payload["snapshot"]).status_code==404


@pytest.mark.parametrize("query",[{"offset":0},{"ref":"main"},{"url":"https://storage.invalid/file"}])
def test_pdf_does_not_accept_raw_resource_or_range_controls(media_browser,query):
    b=media_browser; snap=descriptor(b).json()["snapshot"]
    assert page(b,snap,**query).status_code==400


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    root=tmp_path_factory.mktemp("actual-media-fixtures")
    exe=imageio_ffmpeg.get_ffmpeg_exe()
    video=root/"clip.mp4"
    subprocess.run([exe,"-hide_banner","-loglevel","error","-y","-f","lavfi","-i","color=c=red:s=64x48:r=10:d=13","-f","lavfi","-i","sine=frequency=440:sample_rate=44100:duration=13","-c:v","libx264","-c:a","aac","-pix_fmt","yuv420p","-metadata","title=PRIVATE-SOURCE-TITLE","-shortest",str(video)],check=True,timeout=30)
    audio=root/"clip.wav"
    subprocess.run([exe,"-hide_banner","-loglevel","error","-y","-f","lavfi","-i","sine=frequency=440:sample_rate=44100:duration=3",str(audio)],check=True,timeout=30)
    return {"clip.mp4":video.read_bytes(),"clip.wav":audio.read_bytes()}


@pytest.mark.parametrize("path,kind",[("clip.mp4","video"),("clip.wav","audio")])
def test_actual_media_is_transcoded_to_independent_hls_segments(media_browser,media_files,path,kind):
    b=media_browser;b.contents[path]=media_files[path]
    response=descriptor(b,path)
    assert response.status_code==200,response.text
    data=response.json();assert data["kind"]==kind and data["duration"]>2
    assert set(data)=={"kind","snapshot","playlist_url","duration"}
    playlist=b.client.get(data["playlist_url"])
    assert playlist.status_code==200 and playlist.headers["cache-control"]=="no-store"
    assert playlist.text.startswith("#EXTM3U") and "#EXT-X-ENDLIST" in playlist.text
    assert "BYTERANGE" not in playlist.text and "/resolve/" not in playlist.text and "s3://" not in playlist.text
    urls=[urljoin(data["playlist_url"],line) for line in playlist.text.splitlines() if line and not line.startswith("#")]
    if kind=="video":assert len(urls)>=2
    chunks=[]
    for url in urls:
        result=b.client.get(url);assert result.status_code==200
        assert result.headers["content-type"]=="video/mp2t" and "location" not in result.headers
        assert result.content[0]==0x47
        chunks.append(result.content)
    assert b"".join(chunks)!=media_files[path] and b"PRIVATE-SOURCE-TITLE" not in b"".join(chunks)
    b.storage.get_object_prefix.assert_awaited_once()
    assert not any(name.name=="source.bin" for name in next(iter(media.service.CACHE.entries.values())).directory.iterdir())


def test_pdf_page_positions_are_checked_and_all_pages_can_be_rendered(media_browser):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    second=page(b,snap,2);assert second.status_code==200
    with Image.open(io.BytesIO(second.content)) as im:assert im.height>im.width
    assert page(b,snap,3).status_code==404
    for value in [0,-1,"1.2","../source.bin","x","10000000"]:
        assert page(b,snap,value).status_code==400


@pytest.mark.parametrize("kind",["pdf","hls"])
def test_cached_asset_requests_reject_range_headers(media_browser,media_files,kind):
    b=media_browser;b.contents["clip.mp4"]=media_files["clip.mp4"]
    path="document.pdf" if kind=="pdf" else "clip.mp4";data=descriptor(b,path).json()
    if kind=="pdf":url=b.base+"/media/pdf/page";params={"path":path,"snapshot":data["snapshot"],"page":1}
    else:url=data["playlist_url"];params={}
    assert b.client.get(url,params=params,headers={"Range":"bytes=0-10"}).status_code==400
    if kind=="hls":
        assert b.client.get(b.base+"/media/hls/segment",params={"path":path,"snapshot":data["snapshot"],"segment":0},headers={"Range":"bytes=0-10"}).status_code==400


def test_snapshot_does_not_cross_repository_and_ref_changes_are_detected(media_browser):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    b.storage.get_branch.return_value={"commit_id":"b"*64}
    assert page(b,snap).status_code==409
    b.storage.get_branch.return_value={"commit_id":HEAD}
    invite,code=invites.issue_invite(b.sibling,b.owner,expires_at=utcnow()+timedelta(days=1))
    guest,secret=invites.redeem_guest(b.sibling,code)
    b.client.cookies.set(guest_cookie_name(b.sibling),secret)
    result=b.client.get(b.base.replace("/target/","/sibling/")+"/media/pdf/page",params={"path":"document.pdf","snapshot":snap,"page":1})
    assert result.status_code==409


@pytest.mark.parametrize("mutation",["anonymous","expired","revoked-invite","deleted-file"])
def test_each_media_access_rechecks_real_session_and_file_lifecycle(media_browser,mutation):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    if mutation=="anonymous":b.client.cookies.clear()
    elif mutation=="expired":RepositoryGuestSession.update(expires_at=utcnow()-timedelta(seconds=1)).where(RepositoryGuestSession.id==b.guest.id).execute()
    elif mutation=="revoked-invite":invites.revoke_invite(b.repo,b.invitation.id,b.owner)
    else:make_file(b.repo,"document.pdf","a"*64,is_deleted=True)
    assert page(b,snap).status_code==404


@pytest.mark.parametrize("path",["bad.pdf","bad.mp4","bad.wav"])
def test_invalid_media_returns_safe_decode_error_not_original_bytes(media_browser,path):
    b=media_browser;b.contents[path]=b"PRIVATE secret invalid media"
    result=descriptor(b,path)
    assert result.status_code==422 and "PRIVATE" not in result.text and "secret" not in result.text
    assert not media.service.CACHE.entries


@pytest.mark.parametrize("flag",["missing-row","wrong-owner-hash","deleted-row","owned"])
def test_lfs_media_requires_matching_repository_file_ownership(media_browser,flag):
    b=media_browser;raw=b.contents["document.pdf"];digest=hashlib.sha256(raw).hexdigest()
    b.metadata["document.pdf"]={"physical_address":f"s3://bucket/lfs/{digest[:2]}/{digest[2:4]}/{digest}"}
    if flag!="missing-row":make_file(b.repo,"document.pdf",digest if flag!="wrong-owner-hash" else "f"*64,lfs=True,is_deleted=flag=="deleted-row")
    result=descriptor(b)
    assert result.status_code==(200 if flag=="owned" else 404)
    if flag!="owned":b.storage.get_object_prefix.assert_not_awaited()


def test_size_limits_and_unresolved_pointer_cannot_be_used_to_fetch_raw_content(media_browser):
    b=media_browser;b.metadata["document.pdf"]={"size_bytes":media.service.SOURCE_LIMITS["pdf"]+1}
    assert descriptor(b).status_code==413;b.storage.get_object_prefix.assert_not_awaited()
    b.metadata.clear();b.contents["document.pdf"]=b"version https://git-lfs.github.com/spec/v1\noid sha256:"+b"a"*64
    assert descriptor(b).status_code==422


@pytest.mark.parametrize("value",["","../source.bin","f"*63,"x"*64])
def test_snapshots_are_opaque_validated_identifiers(media_browser,value):
    b=media_browser
    assert page(b,value).status_code==400
    b.storage.get_branch.assert_not_awaited()


def test_unsupported_media_endpoint_rejects_non_media_file(media_browser):
    b=media_browser;snap=media.service.snapshot_id(b.repo,HEAD,"README.md")
    result=b.client.get(b.base+"/media/pdf/page",params={"path":"README.md","snapshot":snap,"page":1})
    assert result.status_code==415



def test_invalid_asset_position_is_rejected_before_expensive_preparation(media_browser):
    b=media_browser;snap=media.service.snapshot_id(b.repo,HEAD,"document.pdf")
    assert page(b,snap,0).status_code==400
    b.storage.get_branch.assert_not_awaited();b.storage.get_object_prefix.assert_not_awaited()


def test_missing_encoder_is_an_actionable_safe_error(media_browser,media_files,monkeypatch):
    b=media_browser;b.contents["clip.mp4"]=media_files["clip.mp4"]
    def unavailable():raise RuntimeError("internal secret binary path")
    monkeypatch.setattr(media.service.imageio_ffmpeg,"get_ffmpeg_exe",unavailable)
    response=descriptor(b,"clip.mp4")
    assert response.status_code==503 and "secret" not in response.text and "encoder" in response.text


def test_lfs_content_hash_is_checked_before_native_processing(media_browser):
    b=media_browser;digest="a"*64
    make_file(b.repo,"document.pdf",digest,lfs=True)
    b.metadata["document.pdf"]={"physical_address":f"s3://bucket/lfs/aa/aa/{digest}"}
    assert descriptor(b).status_code==503
    assert not media.service.CACHE.entries


def test_invalid_storage_address_does_not_escape_as_internal_type_error(media_browser):
    b=media_browser;b.metadata["document.pdf"]={"physical_address":100}
    response=descriptor(b)
    assert response.status_code==503



def test_snapshot_change_during_descriptor_build_discards_the_result(media_browser):
    b=media_browser;b.storage.get_branch.side_effect=[{"commit_id":HEAD},{"commit_id":"b"*64}]
    response=descriptor(b)
    assert response.status_code==409 and "page_count" not in response.text


def test_snapshot_change_during_asset_render_discards_the_page(media_browser):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    b.storage.get_branch.side_effect=[{"commit_id":HEAD},{"commit_id":"b"*64}]
    response=page(b,snap)
    assert response.status_code==409 and not response.content.startswith(b"\x89PNG")


def test_revocation_during_native_page_render_discards_rendered_bytes(media_browser,monkeypatch):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    original=media.service.page_bytes
    async def render_then_revoke(*args):
        result=await original(*args)
        RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id==b.guest.id).execute()
        return result
    monkeypatch.setattr(media.service,"page_bytes",render_then_revoke)
    response=page(b,snap)
    assert response.status_code==404 and not response.content.startswith(b"\x89PNG")


def test_hls_segment_revocation_and_segment_bounds(media_browser,media_files):
    b=media_browser;b.contents["clip.mp4"]=media_files["clip.mp4"];data=descriptor(b,"clip.mp4").json()
    params={"path":"clip.mp4","snapshot":data["snapshot"],"segment":999}
    assert b.client.get(b.base+"/media/hls/segment",params=params).status_code==404
    params["segment"]=0
    assert b.client.get(b.base+"/media/hls/segment",params=params).status_code==200
    RepositoryGuestSession.update(revoked_at=utcnow()).where(RepositoryGuestSession.id==b.guest.id).execute()
    assert b.client.get(b.base+"/media/hls/segment",params=params).status_code==404


def test_media_routes_cannot_treat_a_pdf_as_video_or_video_as_pdf(media_browser,media_files):
    b=media_browser;pdf=descriptor(b).json()
    assert b.client.get(b.base+"/media/hls/playlist",params={"path":"document.pdf","snapshot":pdf["snapshot"]}).status_code==415
    b.contents["clip.mp4"]=media_files["clip.mp4"];video=descriptor(b,"clip.mp4").json()
    assert b.client.get(b.base+"/media/pdf/page",params={"path":"clip.mp4","snapshot":video["snapshot"],"page":1}).status_code==404


@pytest.mark.parametrize("size",[0,-1,None,"123"])
def test_unknown_media_source_size_fails_closed(media_browser,size):
    b=media_browser;b.metadata["document.pdf"]={"size_bytes":size}
    assert descriptor(b).status_code==422;b.storage.get_object_prefix.assert_not_awaited()


@pytest.mark.parametrize("payload",[b"short",{"url":"https://storage.invalid/raw"}])
def test_incomplete_or_nonbyte_source_does_not_become_a_preview(media_browser,payload):
    b=media_browser;b.storage.get_object_prefix.side_effect=None;b.storage.get_object_prefix.return_value=payload
    assert descriptor(b).status_code==503


def test_source_storage_failure_is_redacted_and_releases_processing_capacity(media_browser):
    b=media_browser;b.storage.get_object_prefix.side_effect=RuntimeError("secret object address")
    response=descriptor(b)
    assert response.status_code==503 and "secret" not in response.text
    assert not media.service.CACHE.building


@pytest.mark.parametrize("metadata",[{"path":"other.pdf"},{"path_type":"common_prefix"}])
def test_asset_stat_cannot_redirect_to_another_path(media_browser,metadata):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    b.metadata["document.pdf"]=metadata
    assert page(b,snap).status_code==404


def test_pdf_render_cache_hits_and_oversized_page_are_bounded(media_browser,monkeypatch):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    first=page(b,snap);assert first.status_code==200
    second=page(b,snap);assert second.content==first.content
    monkeypatch.setattr(media.service,"ASSET_LIMIT",1)
    assert page(b,snap).status_code==413



def test_cached_assets_revalidate_storage_metadata_before_serving_any_bytes(media_browser):
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    b.metadata["document.pdf"]={"physical_address":100}
    response=page(b,snap)
    assert response.status_code==503 and not response.content.startswith(b"\x89PNG")



def test_failed_page_worker_cannot_publish_a_partial_cached_png(media_browser,monkeypatch):
    from fastapi import HTTPException
    b=media_browser;snap=descriptor(b).json()["snapshot"]
    entry=media.service.CACHE.entries[snap];target=entry.directory/"page-0001.png"
    original=media.service._pdf
    def failed_worker(directory,operation,page):
        target.write_bytes(b"incomplete-render-output")
        raise HTTPException(422,detail={"error":"Renderer failed"})
    monkeypatch.setattr(media.service,"_pdf",failed_worker)
    assert page(b,snap).status_code==422
    assert not target.exists()
    monkeypatch.setattr(media.service,"_pdf",original)
    assert page(b,snap).content.startswith(b"\x89PNG")
