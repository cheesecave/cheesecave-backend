const MAX_ASSET_BYTES = 256 * 1024;
const MAX_PIXELS = 16_000_000;
const MAX_FRAMES = 200;
const MAX_TOTAL_PIXELS = 64_000_000;

// Read the complete GIF container, rather than searching for a loop signature:
// compressed image bytes and comments can contain the same text. Cache entries
// must also be bounded and structurally valid before they reach an image link.
export function getGifLoop(value) {
  if (typeof value !== "string" || value.length > 350000) return null;
  const match = /^data:image\/gif;base64,([A-Za-z0-9+/]+={0,2})$/.exec(value);
  if (!match || match[1].length % 4 !== 0) return null;
  try {
    const bytes = atob(match[1]);
    if (bytes.length < 14 || bytes.length > MAX_ASSET_BYTES) return null;
    if (!["GIF87a", "GIF89a"].includes(bytes.slice(0, 6))) return null;
    const byte = (index) => bytes.charCodeAt(index);
    const word = (index) => byte(index) | (byte(index + 1) << 8);
    const width = word(6);
    const height = word(8);
    if (!width || !height || width * height > MAX_PIXELS) return null;
    const globalPalette = Boolean(byte(10) & 0x80);
    let offset = 13 + (globalPalette ? 3 * 2 ** ((byte(10) & 7) + 1) : 0);
    let frames = 0;
    let loop = false;
    let loopSeen = false;

    const subBlocks = () => {
      let first = null;
      let total = 0;
      while (offset < bytes.length) {
        const size = byte(offset++);
        if (!size) return { first, total };
        if (offset + size > bytes.length) return null;
        if (first === null) first = bytes.slice(offset, offset + size);
        total += size;
        offset += size;
      }
      return null;
    };

    while (offset < bytes.length) {
      const marker = byte(offset++);
      if (marker === 0x3b) {
        return frames && offset === bytes.length ? loop : null;
      }
      if (marker === 0x2c) {
        if (offset + 9 > bytes.length) return null;
        const frameWidth = word(offset + 4);
        const frameHeight = word(offset + 6);
        const packed = byte(offset + 8);
        if (
          !frameWidth ||
          !frameHeight ||
          frameWidth * frameHeight > MAX_PIXELS ||
          word(offset) + frameWidth > width ||
          word(offset + 2) + frameHeight > height ||
          (!globalPalette && !(packed & 0x80))
        )
          return null;
        offset += 9 + (packed & 0x80 ? 3 * 2 ** ((packed & 7) + 1) : 0);
        if (offset >= bytes.length || byte(offset) < 2 || byte(offset) > 8)
          return null;
        offset++;
        const data = subBlocks();
        if (!data?.total) return null;
        if (++frames > MAX_FRAMES || width * height * frames > MAX_TOTAL_PIXELS)
          return null;
        continue;
      }
      if (marker !== 0x21 || offset >= bytes.length) return null;
      const label = byte(offset++);
      if (label === 0xf9) {
        if (
          offset + 6 > bytes.length ||
          byte(offset) !== 4 ||
          byte(offset + 5) !== 0
        )
          return null;
        offset += 6;
      } else if (label === 0xff) {
        if (offset + 12 > bytes.length || byte(offset) !== 11) return null;
        const application = bytes.slice(offset + 1, offset + 12);
        offset += 12;
        const data = subBlocks();
        if (!data) return null;
        if (["NETSCAPE2.0", "ANIMEXTS1.0"].includes(application)) {
          if (
            loopSeen ||
            data.total !== 3 ||
            data.first?.length !== 3 ||
            data.first.charCodeAt(0) !== 1
          )
            return null;
          loopSeen = true;
          loop =
            data.first.charCodeAt(1) === 0 && data.first.charCodeAt(2) === 0;
        }
      } else if (label === 0x01) {
        if (offset + 13 > bytes.length || byte(offset) !== 12) return null;
        offset += 13;
        if (!subBlocks()) return null;
      } else if (label === 0xfe) {
        if (!subBlocks()) return null;
      } else {
        return null;
      }
    }
    return null;
  } catch {
    return null;
  }
}
