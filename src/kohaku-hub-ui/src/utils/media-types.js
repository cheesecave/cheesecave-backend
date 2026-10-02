/**
 * Browser-renderable media: the one table every preview path reads (the
 * blob page, members inside an indexed tar and their thumbnails, the
 * commit diff, the dataset viewer).
 *
 * Only formats every current browser decodes natively are listed; a file
 * whose decode still fails shows a "cannot display" note, not a broken
 * image. HEIC, JPEG XL and TIFF are left out until they are needed.
 */

// extension -> [kind, MIME]
const MEDIA = {
  jpg: ["image", "image/jpeg"],
  jpeg: ["image", "image/jpeg"],
  jfif: ["image", "image/jpeg"],
  png: ["image", "image/png"],
  apng: ["image", "image/apng"],
  gif: ["image", "image/gif"],
  webp: ["image", "image/webp"],
  avif: ["image", "image/avif"],
  svg: ["image", "image/svg+xml"],
  bmp: ["image", "image/bmp"],
  ico: ["image", "image/x-icon"],
  mp4: ["video", "video/mp4"],
  m4v: ["video", "video/mp4"],
  webm: ["video", "video/webm"],
  mkv: ["video", "video/x-matroska"],
  mov: ["video", "video/quicktime"],
  avi: ["video", "video/x-msvideo"],
  // An .ogg in a dataset is a voice line far more often than a video
  ogg: ["audio", "audio/ogg"],
  oga: ["audio", "audio/ogg"],
  opus: ["audio", "audio/ogg"],
  ogx: ["audio", "application/ogg"],
  mp3: ["audio", "audio/mpeg"],
  wav: ["audio", "audio/wav"],
  flac: ["audio", "audio/flac"],
  m4a: ["audio", "audio/mp4"],
  aac: ["audio", "audio/aac"],
};

export const IMAGE_EXTENSIONS = Object.freeze(
  Object.keys(MEDIA).filter((ext) => MEDIA[ext][0] === "image"),
);

/** The lower-cased extension of the file name, or "" without one. */
export function extensionOf(path) {
  if (typeof path !== "string") return "";
  const name = path.split("/").pop();
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

/** "image" | "video" | "audio", or null for anything else. */
export function mediaKind(path) {
  return MEDIA[extensionOf(path)]?.[0] ?? null;
}

/** The MIME type of a media file, or null for anything else. */
export function mediaMime(path) {
  return MEDIA[extensionOf(path)]?.[1] ?? null;
}

/** A dataset cell holding an image: a data URL, or an http(s) link to one. */
export function looksLikeImageUrl(value) {
  if (typeof value !== "string" || !value) return false;
  if (value.startsWith("data:image/")) return true;
  if (!/^https?:\/\//.test(value)) return false;
  const lower = value.toLowerCase();
  return IMAGE_EXTENSIONS.some((ext) => lower.includes(`.${ext}`));
}
