import { describe, expect, it } from "vitest";

import {
  IMAGE_EXTENSIONS,
  extensionOf,
  looksLikeImageUrl,
  mediaKind,
  mediaMime,
} from "@/utils/media-types";

describe("media types", () => {
  it("takes the extension of the file name, lower-cased", () => {
    expect(extensionOf("images/Cover.AVIF")).toBe("avif");
    expect(extensionOf("a.b/README")).toBe("");
    expect(extensionOf("archive.tar.gz")).toBe("gz");
    expect(extensionOf(null)).toBe("");
  });

  it.each([
    ["sample.avif", "image", "image/avif"],
    ["anim.apng", "image", "image/apng"],
    ["scraped.jfif", "image", "image/jpeg"],
    ["photo.JPG", "image", "image/jpeg"],
    ["a.png", "image", "image/png"],
    ["a.gif", "image", "image/gif"],
    ["a.webp", "image", "image/webp"],
    ["a.svg", "image", "image/svg+xml"],
    ["a.bmp", "image", "image/bmp"],
    ["a.ico", "image", "image/x-icon"],
    // Audio first: an .ogg in a dataset is a voice line, not a video
    ["voice.ogg", "audio", "audio/ogg"],
    ["voice.oga", "audio", "audio/ogg"],
    ["voice.opus", "audio", "audio/ogg"],
    ["voice.ogx", "audio", "application/ogg"],
    ["a.mp3", "audio", "audio/mpeg"],
    ["a.wav", "audio", "audio/wav"],
    ["a.flac", "audio", "audio/flac"],
    ["a.m4a", "audio", "audio/mp4"],
    ["a.aac", "audio", "audio/aac"],
    ["clip.mkv", "video", "video/x-matroska"],
    ["clip.m4v", "video", "video/mp4"],
    ["a.mp4", "video", "video/mp4"],
    ["a.webm", "video", "video/webm"],
    ["a.mov", "video", "video/quicktime"],
    ["a.avi", "video", "video/x-msvideo"],
  ])("%s is %s (%s)", (path, kind, mime) => {
    expect(mediaKind(path)).toBe(kind);
    expect(mediaMime(path)).toBe(mime);
  });

  it("knows nothing of other files, nor of formats left out on purpose", () => {
    for (const path of ["model.safetensors", "README", "a.heic", "a.jxl", "a.tiff", null]) {
      expect(mediaKind(path)).toBeNull();
      expect(mediaMime(path)).toBeNull();
    }
  });

  it("lists the image extensions", () => {
    expect(IMAGE_EXTENSIONS).toEqual(
      expect.arrayContaining(["avif", "apng", "jfif", "jpg", "png", "webp"]),
    );
    expect(IMAGE_EXTENSIONS).not.toContain("mp4");
  });

  it("recognises image links in dataset cells", () => {
    expect(looksLikeImageUrl("data:image/avif;base64,AAAA")).toBe(true);
    expect(looksLikeImageUrl("https://cdn.example.com/a/b.avif?x=1")).toBe(true);
    expect(looksLikeImageUrl("http://example.com/x.JFIF")).toBe(true);
    expect(looksLikeImageUrl("https://example.com/clip.mp4")).toBe(false);
    expect(looksLikeImageUrl("ftp://example.com/a.png")).toBe(false);
    expect(looksLikeImageUrl("")).toBe(false);
    expect(looksLikeImageUrl(null)).toBe(false);
    expect(looksLikeImageUrl(42)).toBe(false);
  });
});
