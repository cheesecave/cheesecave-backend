"""Isolated PDF rendering worker. Never sends a source document to a browser."""

import json
import math
from pathlib import Path
import sys

import pypdfium2 as pdfium


def render_scale(width, height):
    if not all(math.isfinite(v) and 0 < v <= 100000 for v in (width, height)):
        raise ValueError("Invalid PDF page size")
    return min(1600 / max(width, height), 3.0)


def main():
    operation, root_name, page_number = sys.argv[1:]
    root = Path(root_name)
    with pdfium.PdfDocument(root / "source.bin") as document:
        count = len(document)
        if count < 1 or count > 1000:
            raise ValueError("PDF page limit")
        if operation == "pdf-info":
            (root / "metadata.json").write_text(json.dumps({"page_count": count}), encoding="utf-8")
            return
        number = int(page_number)
        if operation != "pdf-page" or number < 1 or number > count:
            raise ValueError("Invalid PDF page")
        page = document[number - 1]
        try:
            width, height = page.get_size()
            scale = render_scale(width, height)
            bitmap = page.render(scale=scale, may_draw_forms=False)
            try:
                image = bitmap.to_pil().convert("RGB")
                try:
                    image.save(root / f"page-{number:04d}.png", format="PNG", optimize=True)
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()


if __name__ == "__main__":
    main()
