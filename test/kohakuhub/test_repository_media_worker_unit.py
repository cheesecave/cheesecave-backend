"""PDF worker geometry and lifecycle using genuine generated PDF documents."""

import json
import sys

import pytest
import pypdfium2 as pdfium
from PIL import Image

from kohakuhub import media_worker as worker
from test.kohakuhub.api.test_repository_guest_media_unit import pdf_bytes


def test_worker_metadata_and_page_render_use_the_same_actual_pdf(tmp_path,monkeypatch):
    (tmp_path/"source.bin").write_bytes(pdf_bytes())
    monkeypatch.setattr(sys,"argv",["worker","pdf-info",str(tmp_path),"0"])
    worker.main()
    assert json.loads((tmp_path/"metadata.json").read_text())["page_count"]==2
    monkeypatch.setattr(sys,"argv",["worker","pdf-page",str(tmp_path),"1"])
    worker.main()
    with Image.open(tmp_path/"page-0001.png") as image:assert image.size==(600,300)


@pytest.mark.parametrize("operation,page",[("unknown","1"),("pdf-page","0"),("pdf-page","3")])
def test_worker_rejects_invalid_internal_page_operations(tmp_path,monkeypatch,operation,page):
    (tmp_path/"source.bin").write_bytes(pdf_bytes())
    monkeypatch.setattr(sys,"argv",["worker",operation,str(tmp_path),page])
    with pytest.raises(ValueError,match="Invalid PDF page"):worker.main()


def test_worker_page_count_limit_is_checked_with_a_real_document(tmp_path,monkeypatch):
    document=pdfium.PdfDocument.new()
    try:
        for _ in range(1001):document.new_page(20,20).close()
        document.save(tmp_path/"source.bin")
    finally:document.close()
    monkeypatch.setattr(sys,"argv",["worker","pdf-info",str(tmp_path),"0"])
    with pytest.raises(ValueError,match="page limit"):worker.main()


@pytest.mark.parametrize("width,height",[(0,100),(-1,100),(100,0),(float("nan"),100),(100,float("inf")),(100001,100)])
def test_page_raster_geometry_is_bounded_before_native_allocation(width,height):
    with pytest.raises(ValueError):worker.render_scale(width,height)


def test_large_and_small_valid_pages_use_a_fixed_safe_pixel_ceiling():
    assert worker.render_scale(10000,20000)==0.08
    assert worker.render_scale(100,200)==3.0



def test_real_worker_script_entry_point_executes_its_pdf_operation(tmp_path,monkeypatch):
    import runpy
    (tmp_path/"source.bin").write_bytes(pdf_bytes())
    monkeypatch.setattr(sys,"argv",[worker.__file__,"pdf-info",str(tmp_path),"0"])
    runpy.run_path(worker.__file__,run_name="__main__")
    assert json.loads((tmp_path/"metadata.json").read_text())["page_count"]==2
