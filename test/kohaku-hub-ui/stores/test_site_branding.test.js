import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { useSiteBrandingStore } from "@/stores/siteBranding";
import {
  applyDocumentBranding,
  CACHE_KEY,
  DEFAULT_BRANDING,
  fetchBranding,
  getGifLoop,
  normalizeBranding,
  readCachedBranding,
  saveCachedBranding,
} from "../../../src/shared/site-branding.js";

const png =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==";
const svgData = (xml) => `data:image/svg+xml;base64,${btoa(xml)}`;
const svg = svgData(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><defs><linearGradient id="color"><stop stop-color="#fc0"/></linearGradient></defs><path fill="url(#color)" d="M0 0h16v16H0z"/></svg>',
);
const gifBytes = (loop = null, application = "NETSCAPE2.0") => {
  const header = "GIF89a" + String.fromCharCode(1, 0, 1, 0, 0x80, 0, 0);
  const palette = String.fromCharCode(255, 0, 0, 0, 0, 255);
  const extension =
    loop === null
      ? ""
      : String.fromCharCode(0x21, 0xff, 11) +
        application +
        String.fromCharCode(3, 1, loop & 255, loop >> 8, 0);
  const frame = (color) =>
    String.fromCharCode(
      0x21,
      0xf9,
      4,
      8,
      10,
      0,
      0,
      0,
      0x2c,
      0,
      0,
      0,
      0,
      1,
      0,
      1,
      0,
      0,
      2,
      2,
      color,
      1,
      0,
    );
  return header + palette + extension + frame(0x44) + frame(0x4c) + ";";
};
const gifData = (bytes) => `data:image/gif;base64,${btoa(bytes)}`;
const loopingGif = gifData(gifBytes(0));
const onceGif = gifData(gifBytes());
const custom = {
  site_name: "DeepGHS Hub",
  footer_description: "Models from our community",
  header_logo: png,
  favicon: png,
};
const response = (value, options = {}) => ({
  ok: true,
  headers: new Headers(),
  json: async () => value,
  ...options,
});

describe("site branding resilience", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.head
      .querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]')
      .forEach((link) => link.remove());
  });
  afterEach(() => vi.useRealTimers());

  it("renders bundled defaults during a cold offline start", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const store = useSiteBrandingStore();
    const dispose = store.initialize();
    expect(store.branding).toEqual(DEFAULT_BRANDING);
    expect(document.title).toBe("KohakuHub");
    expect(
      document.querySelector('link[rel="icon"]').getAttribute("href"),
    ).toBe("/favicon.svg");
    await flushPromises();
    expect(store.branding).toEqual(DEFAULT_BRANDING);
    dispose();
  });

  it("restores cached names and inline images synchronously and retains them offline", async () => {
    saveCachedBranding(custom);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const store = useSiteBrandingStore();
    const dispose = store.initialize();
    expect(store.branding).toEqual(custom);
    expect(document.title).toBe(custom.site_name);
    expect(
      document.querySelector('link[rel="icon"]').getAttribute("href"),
    ).toBe(png);
    expect(await store.refresh()).toBe(false);
    expect(readCachedBranding()).toEqual(custom);
    dispose();
  });

  it("preserves cache when the API uses its database-failure fallback", async () => {
    saveCachedBranding(custom);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response(DEFAULT_BRANDING, {
          headers: new Headers({ "X-Site-Branding-Fallback": "true" }),
        }),
      ),
    );
    const store = useSiteBrandingStore();
    expect(await store.refresh()).toBe(false);
    expect(store.branding).toEqual(custom);
    expect(readCachedBranding()).toEqual(custom);
  });

  it("updates document, cache, and store after a successful refresh", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(custom)));
    const store = useSiteBrandingStore();
    expect(await store.refresh()).toBe(true);
    expect(store.branding).toEqual(custom);
    expect(readCachedBranding()).toEqual(custom);
    expect(
      document.querySelector('link[rel="icon"]').getAttribute("type"),
    ).toBe("image/png");
  });

  it("restores vector logos and favicons from the v1 cache during an outage", async () => {
    const vectorBranding = { ...custom, header_logo: svg, favicon: svg };
    expect(saveCachedBranding(vectorBranding)).toBe(true);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const store = useSiteBrandingStore();
    const dispose = store.initialize();
    expect(store.branding).toEqual(vectorBranding);
    expect(document.querySelector('link[rel="icon"]').href).toBe(svg);
    expect(document.querySelector('link[rel="icon"]').type).toBe(
      "image/svg+xml",
    );
    expect(document.querySelector('link[rel="apple-touch-icon"]').href).toBe(
      svg,
    );
    await flushPromises();
    expect(readCachedBranding()).toEqual(vectorBranding);
    dispose();
  });

  it.each([loopingGif, onceGif])(
    "restores complete animated GIFs and their playback mode offline: %s",
    async (image) => {
      const animated = { ...custom, header_logo: image, favicon: image };
      expect(saveCachedBranding(animated)).toBe(true);
      vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
      const store = useSiteBrandingStore();
      const dispose = store.initialize();
      expect(store.branding).toEqual(animated);
      expect(document.querySelector('link[rel="icon"]').href).toBe(image);
      expect(document.querySelector('link[rel="icon"]').type).toBe("image/gif");
      await flushPromises();
      expect(readCachedBranding()).toEqual(animated);
      expect(getGifLoop(readCachedBranding().header_logo)).toBe(
        image === loopingGif,
      );
      dispose();
    },
  );

  it("reads GIF loop extensions without mistaking frame/comment data for a mode", () => {
    expect(getGifLoop(loopingGif)).toBe(true);
    expect(getGifLoop(onceGif)).toBe(false);
    expect(getGifLoop(gifData(gifBytes(0, "ANIMEXTS1.0")))).toBe(true);
    expect(getGifLoop(gifData(gifBytes(3)))).toBe(false);
    expect(getGifLoop(gifData(gifBytes().replace("GIF89a", "GIF87a")))).toBe(
      false,
    );
    expect(getGifLoop(png)).toBeNull();
    expect(getGifLoop(svg)).toBeNull();
    const comment = "NETSCAPE2.0" + String.fromCharCode(3, 1, 0, 0, 0);
    const bytes =
      gifBytes().slice(0, -1) +
      String.fromCharCode(0x21, 0xfe, comment.length) +
      comment +
      "\0;";
    expect(getGifLoop(gifData(bytes))).toBe(false);
  });

  it("retains a play-once favicon URL when only site text changes", () => {
    const animated = { ...custom, favicon: onceGif };
    applyDocumentBranding(animated);
    const icon = document.querySelector('link[rel="icon"]');
    const setter = vi.spyOn(icon, "href", "set");
    const typeSetter = vi.spyOn(icon, "type", "set");
    applyDocumentBranding({ ...animated, site_name: "Changed title" });
    expect(document.title).toBe("Changed title");
    expect(setter).not.toHaveBeenCalled();
    expect(typeSetter).not.toHaveBeenCalled();
    applyDocumentBranding({ ...animated, favicon: loopingGif });
    expect(setter).toHaveBeenCalledWith(loopingGif);
  });

  it("bounds GIF frame counts independently of the compressed payload size", () => {
    const bytes = gifBytes();
    const pair = bytes.slice(19, -1);
    const data = (count) =>
      gifData(bytes.slice(0, 19) + pair.repeat(count) + ";");
    expect(getGifLoop(data(100))).toBe(false);
    expect(getGifLoop(data(101))).toBeNull();
  });

  it("rejects truncated, oversized and structurally invalid GIF cache entries", () => {
    const bytes = gifBytes(0);
    const oversizedComment = "x".repeat(255);
    const tooLarge =
      bytes.slice(0, -1) +
      String.fromCharCode(0x21, 0xfe) +
      (String.fromCharCode(255) + oversizedComment).repeat(1024) +
      "\0;";
    for (const image of [
      gifData("GIF89a"),
      gifData(bytes.slice(0, -1)),
      gifData(bytes + "extra"),
      gifData(bytes.replace("GIF89a", "GIF90a")),
      gifData("GIF89a" + String.fromCharCode(0, 0) + bytes.slice(8)),
      gifData(bytes.slice(0, 13) + String.fromCharCode(0x21, 0xfe, 255)),
      gifData(tooLarge),
      "data:image/gif;base64,invalid=base64",
    ]) {
      expect(getGifLoop(image)).toBeNull();
      const invalid = { ...custom, favicon: image };
      expect(normalizeBranding(invalid)).toBeNull();
      localStorage.setItem(
        CACHE_KEY,
        JSON.stringify({ version: 1, branding: invalid }),
      );
      expect(readCachedBranding()).toEqual(DEFAULT_BRANDING);
    }
  });

  it("rejects arbitrary URLs, malformed payloads, and oversized images", () => {
    for (const change of [
      { header_logo: "https://example.com/logo.png" },
      { favicon: "data:image/svg+xml;base64,PHN2Zz4=" },
      { favicon: "data:image/png;base64,iVBORw0KGgo;bad" },
      { header_logo: `${png}${"A".repeat(350000)}` },
      { site_name: "x".repeat(101) },
      { site_name: " " },
      { footer_description: "x".repeat(2001) },
      { header_logo: undefined },
    ]) {
      expect(normalizeBranding({ ...custom, ...change })).toBeNull();
    }
    expect(normalizeBranding(null)).toBeNull();
    expect(normalizeBranding([])).toBeNull();
  });

  it.each([
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><html/></foreignObject></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><use href="https://example.com/logo.svg#x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://example.com/gradient)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com/a.css";</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>path{fill:u\\72l(https://example.com/a)}</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x "bad">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
    '<svg xmlns="https://example.com/svg"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><?external test?></svg>',
  ])("rejects unsafe SVG in server data and cached branding: %s", (xml) => {
    const unsafe = { ...custom, favicon: svgData(xml) };
    expect(normalizeBranding(unsafe)).toBeNull();
    localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ version: 1, branding: unsafe }),
    );
    expect(readCachedBranding()).toEqual(DEFAULT_BRANDING);
  });

  it("supports independent SVG and PNG branding assets", () => {
    expect(normalizeBranding({ ...custom, header_logo: svg })).toEqual({
      ...custom,
      header_logo: svg,
    });
    applyDocumentBranding({ ...custom, favicon: svg }, { admin: true });
    expect(document.querySelector('link[rel="icon"]').type).toBe(
      "image/svg+xml",
    );
    applyDocumentBranding(custom);
    expect(document.querySelector('link[rel="icon"]').type).toBe("image/png");
  });

  it.each([
    '<ns0:svg xmlns:ns0="http://www.w3.org/2000/svg" xmlns:ns1="http://www.w3.org/1999/xlink"><ns0:defs><ns0:path id="shape" d="M0 0h8v8H0z"/></ns0:defs><ns0:use ns1:href="#shape"/></ns0:svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>.logo{fill:u\\72l("#paint");stroke:rgb(255,0,0)}</style><defs><linearGradient id="paint"><stop stop-color="#000"/></linearGradient></defs><path class="logo" d="M0 0h8v8H0z"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text style="font-family:\'url(https://example.com/font)\'">Logo</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>text{font-family:"url(https://example.com/font)"}</style><text>Logo</text></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><path style="fill: /* comment */ url(\'#paint\')"/></svg>',
  ])(
    "accepts normalized static SVG namespaces, styles and local references: %s",
    (xml) => {
      const image = svgData(xml);
      expect(
        normalizeBranding({ ...custom, header_logo: image, favicon: image }),
      ).toEqual({
        ...custom,
        header_logo: image,
        favicon: image,
      });
    },
  );

  it("bounds decoded SVG size and rejects malformed base64 and UTF-8", () => {
    const large = svgData(
      `<svg xmlns="http://www.w3.org/2000/svg"><desc>${"x".repeat(256 * 1024)}</desc></svg>`,
    );
    for (const image of [
      large,
      "data:image/svg+xml;base64,/w==",
      "data:image/svg+xml;base64,invalid=base64",
      `${svg}!`,
    ]) {
      expect(normalizeBranding({ ...custom, header_logo: image })).toBeNull();
    }
  });

  it("uses defaults for corrupt or incompatible cache entries", () => {
    for (const raw of [
      "bad-json",
      "null",
      JSON.stringify(custom),
      JSON.stringify({ version: 2, branding: custom }),
      JSON.stringify({ version: 1, branding: { ...custom, favicon: "bad" } }),
    ]) {
      localStorage.setItem(CACHE_KEY, raw);
      expect(readCachedBranding()).toEqual(DEFAULT_BRANDING);
    }
  });

  it("continues in memory when local storage is blocked or full", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(readCachedBranding()).toEqual(DEFAULT_BRANDING);
    expect(saveCachedBranding(custom)).toBe(false);
    const store = useSiteBrandingStore();
    expect(store.apply(custom)).toBe(true);
    expect(document.title).toBe(custom.site_name);
    expect(store.branding).toEqual(custom);
  });

  it("retains saved configuration for invalid server responses or HTTP failures", async () => {
    saveCachedBranding(custom);
    const store = useSiteBrandingStore();
    for (const value of [
      response({ ...custom, favicon: "bad" }),
      response(DEFAULT_BRANDING, { ok: false }),
    ]) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(value));
      expect(await store.refresh()).toBe(false);
      expect(readCachedBranding()).toEqual(custom);
    }
  });

  it("bounds requests even if fetch does not respond to abort", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const request = fetchBranding();
    const rejected = expect(request).rejects.toThrow("timed out");
    await vi.advanceTimersByTimeAsync(3000);
    await rejected;
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it("bounds response JSON reading as part of the request deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          response(null, { json: () => new Promise(() => {}) }),
        ),
    );
    const request = fetchBranding();
    const rejected = expect(request).rejects.toThrow("timed out");
    await vi.advanceTimersByTimeAsync(3000);
    await rejected;
  });

  it("receives admin saves from other tabs and removes the listener on disposal", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const store = useSiteBrandingStore();
    const dispose = store.initialize();
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: CACHE_KEY,
        newValue: JSON.stringify({ version: 1, branding: custom }),
      }),
    );
    expect(store.branding).toEqual(custom);
    expect(document.title).toBe(custom.site_name);
    dispose();
    window.dispatchEvent(
      new StorageEvent("storage", { key: CACHE_KEY, newValue: null }),
    );
    expect(store.branding).toEqual(custom);
    await flushPromises();
  });

  it("restores default icons and independently supports the admin title and assets", () => {
    applyDocumentBranding(custom, { admin: true });
    expect(document.title).toBe("DeepGHS Hub Admin Portal");
    expect(
      document
        .querySelector('link[rel="apple-touch-icon"]')
        .getAttribute("href"),
    ).toBe(png);
    applyDocumentBranding(DEFAULT_BRANDING, { admin: true });
    expect(document.title).toBe("KohakuHub Admin Portal");
    expect(
      document.querySelector('link[rel="icon"]').getAttribute("href"),
    ).toBe("/admin/favicon.svg");
    expect(
      document
        .querySelector('link[rel="apple-touch-icon"]')
        .getAttribute("href"),
    ).toBe("/admin/images/logo-square.svg");
  });

  it("does not overwrite a newer admin save with an older public response", async () => {
    let finishFetch;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((resolve) => {
            finishFetch = resolve;
          }),
      ),
    );
    const store = useSiteBrandingStore();
    const pending = store.refresh();
    store.apply(custom);
    finishFetch(response(DEFAULT_BRANDING));
    expect(await pending).toBe(false);
    expect(store.branding).toEqual(custom);
    expect(readCachedBranding()).toEqual(custom);
  });
});
