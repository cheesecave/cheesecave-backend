import { createPinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSiteBrandingStore } from "@/stores/siteBranding";
import {
  CACHE_KEY,
  DEFAULT_BRANDING,
  saveCachedBranding,
} from "../../../src/shared/site-branding.js";

const mocks = vi.hoisted(() => ({ fetchBranding: vi.fn() }));
vi.mock("../../../src/shared/site-branding.js", async (importOriginal) => ({
  ...(await importOriginal()),
  fetchBranding: (...args) => mocks.fetchBranding(...args),
}));

describe("admin branding store", () => {
  let pinia;
  beforeEach(() => {
    pinia = createPinia();
    vi.clearAllMocks();
    mocks.fetchBranding.mockResolvedValue({
      ...DEFAULT_BRANDING,
      site_name: "Server Hub",
    });
  });
  afterEach(() => pinia._s.forEach((store) => store.$dispose()));

  it("restores cached branding before the public request and retains it offline", async () => {
    saveCachedBranding({ ...DEFAULT_BRANDING, site_name: "Cached Hub" });
    const store = useSiteBrandingStore(pinia);
    expect(store.branding.site_name).toBe("Cached Hub");
    expect(document.title).toBe("Cached Hub Admin Portal");
    mocks.fetchBranding.mockRejectedValue(new Error("Offline"));
    await expect(store.refresh()).resolves.toBeUndefined();
    expect(store.branding.site_name).toBe("Cached Hub");
  });

  it("retains packaged defaults when there is no cache and the backend fails", async () => {
    mocks.fetchBranding.mockRejectedValue(new Error("Offline"));
    const store = useSiteBrandingStore(pinia);
    await store.refresh();
    expect(store.branding).toEqual(DEFAULT_BRANDING);
    expect(
      document.querySelector('link[rel="icon"]').getAttribute("href"),
    ).toBe("/admin/favicon.svg");
  });

  it("updates the document and cached values after a successful refresh", async () => {
    const store = useSiteBrandingStore(pinia);
    await store.refresh();
    expect(store.branding.site_name).toBe("Server Hub");
    expect(document.title).toBe("Server Hub Admin Portal");
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)).branding.site_name).toBe(
      "Server Hub",
    );
  });

  it("does not overwrite newer admin edits with a delayed public response", async () => {
    let resolveFetch;
    mocks.fetchBranding.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = useSiteBrandingStore(pinia);
    const pending = store.refresh();
    store.apply({ ...DEFAULT_BRANDING, site_name: "Saved Hub" });
    resolveFetch({ ...DEFAULT_BRANDING, site_name: "Old Hub" });
    await pending;
    expect(store.branding.site_name).toBe("Saved Hub");
  });

  it("synchronizes updates from another tab and stops listening after disposal", () => {
    const store = useSiteBrandingStore(pinia);
    saveCachedBranding({ ...DEFAULT_BRANDING, site_name: "Other Tab" });
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: CACHE_KEY,
        newValue: localStorage.getItem(CACHE_KEY),
      }),
    );
    expect(store.branding.site_name).toBe("Other Tab");
    expect(document.title).toBe("Other Tab Admin Portal");
    store.$dispose();
    saveCachedBranding({ ...DEFAULT_BRANDING, site_name: "Later" });
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: CACHE_KEY,
        newValue: localStorage.getItem(CACHE_KEY),
      }),
    );
    expect(store.branding.site_name).toBe("Other Tab");
  });

  it("keeps the current branding when another tab writes a corrupt cache", () => {
    const store = useSiteBrandingStore(pinia);
    store.apply({ ...DEFAULT_BRANDING, site_name: "Saved Hub" });
    window.dispatchEvent(
      new StorageEvent("storage", { key: CACHE_KEY, newValue: "{broken" }),
    );
    expect(store.branding.site_name).toBe("Saved Hub");
    expect(document.title).toBe("Saved Hub Admin Portal");
  });

  it("does not overwrite a newer storage event with a delayed public response", async () => {
    let resolveFetch;
    mocks.fetchBranding.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = useSiteBrandingStore(pinia);
    const pending = store.refresh();
    saveCachedBranding({ ...DEFAULT_BRANDING, site_name: "Other Tab" });
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: CACHE_KEY,
        newValue: localStorage.getItem(CACHE_KEY),
      }),
    );
    resolveFetch({ ...DEFAULT_BRANDING, site_name: "Old Hub" });
    await pending;
    expect(store.branding.site_name).toBe("Other Tab");
  });
});
