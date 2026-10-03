"""Bounded GIF animation normalization and lossless playback-mode changes."""

from io import BytesIO

from fastapi import HTTPException
from PIL import Image

MAX_GIF_FRAMES = 200
MAX_GIF_TOTAL_PIXELS = 64_000_000


def normalize_gif(source: Image.Image, edge: int, loop: bool) -> bytes:
    """Encode composited frames, preserving timing and transparent backgrounds."""
    frame_count = source.n_frames
    if frame_count > MAX_GIF_FRAMES:
        raise HTTPException(400, detail="GIF must contain at most 200 frames")
    if source.width * source.height * frame_count > MAX_GIF_TOTAL_PIXELS:
        raise HTTPException(400, detail="GIF decoded frames must total at most 64 million pixels")
    frames = []
    durations = []
    for index in range(frame_count):
        source.seek(index)
        frame = source.convert("RGBA")
        frame.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        # Reserve palette index 255 for transparency in every full, composited
        # frame. Disposal 2 clears the prior frame before the next one appears.
        palette_frame = frame.convert("RGB").quantize(colors=255)
        transparent = frame.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0)
        palette_frame.paste(255, mask=transparent)
        palette_frame.info = {"transparency": 255}
        frames.append(palette_frame)
        durations.append(source.info.get("duration", 0))
    output = BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        disposal=2,
        transparency=255,
        optimize=False,
    )
    return set_gif_loop(output.getvalue(), loop)


def set_gif_loop(contents: bytes, loop: bool) -> bytes:
    """Edit only loop application extensions; retain compressed frame bytes."""
    try:
        if len(contents) < 14 or contents[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ValueError("Missing GIF header")
        packed = contents[10]
        position = 13 + (3 * (2 ** ((packed & 7) + 1)) if packed & 128 else 0)
        if position >= len(contents):
            raise ValueError("Truncated GIF color table")
        result = bytearray(contents[:position])
        if loop:
            result[:6] = b"GIF89a"
            result.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")

        def skip_subblocks(start):
            while True:
                size = contents[start]
                start += 1
                if size == 0:
                    return start
                start += size
                if start >= len(contents):
                    raise ValueError("Truncated GIF data")

        while position < len(contents):
            start = position
            marker = contents[position]
            position += 1
            if marker == 0x3B:
                if position != len(contents):
                    raise ValueError("Unexpected data after GIF trailer")
                result.append(marker)
                return bytes(result)
            if marker == 0x21:
                label = contents[position]
                position += 1
                is_loop = (
                    label == 0xFF
                    and contents[position] == 11
                    and contents[position + 1 : position + 12] in {b"NETSCAPE2.0", b"ANIMEXTS1.0"}
                )
                position = skip_subblocks(position)
                if is_loop:
                    continue
            elif marker == 0x2C:
                packed = contents[position + 8]
                position += 9
                if packed & 128:
                    position += 3 * (2 ** ((packed & 7) + 1))
                position = skip_subblocks(position + 1)  # Skip LZW minimum code size.
            else:
                raise ValueError("Invalid GIF block")
            result.extend(contents[start:position])
        raise ValueError("Missing GIF trailer")
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, detail="Invalid stored GIF animation") from exc
