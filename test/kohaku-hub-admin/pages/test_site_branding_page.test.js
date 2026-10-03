import { defineComponent, h } from "vue";
import { createPinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ElementPlusStubs } from "../helpers/vue";
import { useSiteBrandingStore } from "@/stores/siteBranding";
import { CACHE_KEY } from "../../../src/shared/site-branding.js";

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn() },
  adminStore: { token: "admin-token", logout: vi.fn() },
  getSiteBranding: vi.fn(),
  updateSiteBranding: vi.fn(),
  uploadSiteBrandingAsset: vi.fn(),
  updateSiteBrandingAssetAnimation: vi.fn(),
  resetSiteBrandingAsset: vi.fn(),
}));
vi.mock("vue-router", () => ({ useRouter: () => mocks.router }));
vi.mock("@/stores/admin", () => ({ useAdminStore: () => mocks.adminStore }));
vi.mock("@/utils/api", () => ({
  getSiteBranding: (...args) => mocks.getSiteBranding(...args),
  updateSiteBranding: (...args) => mocks.updateSiteBranding(...args),
  uploadSiteBrandingAsset: (...args) => mocks.uploadSiteBrandingAsset(...args),
  updateSiteBrandingAssetAnimation: (...args) =>
    mocks.updateSiteBrandingAssetAnimation(...args),
  resetSiteBrandingAsset: (...args) => mocks.resetSiteBrandingAsset(...args),
}));
vi.mock("@/components/AdminLayout.vue", () => ({
  default: defineComponent({
    setup:
      (_, { slots }) =>
      () =>
        h("main", slots.default?.()),
  }),
}));

import SiteBrandingPage from "@/pages/site-branding.vue";

const original = {
  site_name: "DeepGHS Hub",
  footer_description: "Models for everyone",
  header_logo: null,
  favicon: null,
};
const image =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=";
const svgText =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path fill="#fa4" d="M0 0h16v16H0z"/></svg>';
const svgImage = `data:image/svg+xml;base64,${btoa(svgText)}`;
const gifLoop =
  "data:image/gif;base64,R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAAAgACAAAIBgABCAQQEAAh+QQBFAABACwAAAAAAgACAIEAAP8AAAAAAAAAAAAIBgABCAQQEAA7";
const gifOnce =
  "data:image/gif;base64,R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH5BAAKAAAALAAAAAACAAIAAAgGAAEIBBAQACH5BAEUAAEALAAAAAACAAIAgQAA/wAAAAAAAAAAAAgGAAEIBBAQADs=";

describe("site branding administration", () => {
  let wrapper;
  let pinia;
  let success;

  beforeEach(async () => {
    vi.clearAllMocks();
    mocks.adminStore.token = "admin-token";
    mocks.getSiteBranding.mockResolvedValue({ ...original });
    mocks.updateSiteBranding.mockResolvedValue({ ...original });
    mocks.uploadSiteBrandingAsset.mockResolvedValue({ ...original });
    mocks.updateSiteBrandingAssetAnimation.mockResolvedValue({ ...original });
    mocks.resetSiteBrandingAsset.mockResolvedValue({ ...original });
    const { ElMessage } = await vi.importActual("element-plus");
    success = vi.spyOn(ElMessage, "success").mockImplementation(() => {});
    vi.spyOn(ElMessage, "error").mockImplementation(() => {});
    pinia = createPinia();
  });
  afterEach(() => {
    wrapper?.unmount();
    pinia._s.forEach((store) => store.$dispose());
  });
  function mountPage() {
    wrapper = mount(SiteBrandingPage, {
      global: { plugins: [pinia], stubs: ElementPlusStubs },
    });
    return wrapper;
  }
  async function chooseFile(asset, file) {
    Object.defineProperty(wrapper.get(`#${asset}`).element, "files", {
      value: [file],
      configurable: true,
    });
    await wrapper.get(`#${asset}`).trigger("change");
    await flushPromises();
  }
  function resetButton(asset) {
    return wrapper
      .findAll("button")
      .find(
        (button) =>
          button.attributes("aria-label") ===
          `Restore ${asset === "favicon" ? "favicon" : "header logo"}`,
      );
  }
  function playbackButton(asset) {
    return wrapper
      .findAll("button")
      .find(
        (button) =>
          button.attributes("aria-label") ===
          `Save ${asset === "favicon" ? "favicon" : "header logo"} playback`,
      );
  }

  it("loads authenticated settings, then saves text and updates the shared cache", async () => {
    mountPage();
    await flushPromises();
    expect(mocks.getSiteBranding).toHaveBeenCalledWith("admin-token");
    expect(wrapper.get("#site-name").element.value).toBe(original.site_name);
    await wrapper.get("#site-name").setValue(" My Hub ");
    await wrapper.get("#footer-description").setValue("");
    mocks.updateSiteBranding.mockResolvedValue({
      ...original,
      site_name: "My Hub",
      footer_description: "",
    });
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(mocks.updateSiteBranding).toHaveBeenCalledWith("admin-token", {
      site_name: "My Hub",
      footer_description: "",
    });
    expect(useSiteBrandingStore(pinia).branding.site_name).toBe("My Hub");
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)).branding.site_name).toBe(
      "My Hub",
    );
    expect(document.title).toContain("My Hub");
    expect(success).toHaveBeenCalledOnce();
  });

  it("disables all mutations while loading and after read failure, then permits retry", async () => {
    let rejectRead;
    mocks.getSiteBranding.mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectRead = reject;
        }),
    );
    mountPage();
    expect(wrapper.get("fieldset").element.disabled).toBe(true);
    expect(wrapper.get("#favicon").element.disabled).toBe(true);
    await wrapper.get("form").trigger("submit");
    expect(mocks.updateSiteBranding).not.toHaveBeenCalled();
    rejectRead(new Error("Backend offline"));
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Backend offline");
    expect(wrapper.get("fieldset").element.disabled).toBe(true);
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Reload")
      .trigger("click");
    await flushPromises();
    expect(wrapper.get("fieldset").element.disabled).toBe(false);
  });

  it("keeps saved branding and the unsaved draft on save failure", async () => {
    mountPage();
    await flushPromises();
    await wrapper.get("#site-name").setValue("Unsaved");
    mocks.updateSiteBranding.mockRejectedValue(new Error("Save failed"));
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Save failed");
    expect(useSiteBrandingStore(pinia).branding.site_name).toBe(
      original.site_name,
    );
    expect(wrapper.get("#site-name").element.value).toBe("Unsaved");
    expect(success).not.toHaveBeenCalled();
  });

  it("uploads and resets each asset separately without saving the text draft", async () => {
    mountPage();
    await flushPromises();
    await wrapper.get("#site-name").setValue("Unsaved name");
    for (const asset of ["header_logo", "favicon"]) {
      const label = asset === "favicon" ? "favicon" : "header logo";
      const input = wrapper.get(`#${asset}`);
      expect(input.attributes()).toHaveProperty("hidden");
      const openPicker = vi
        .spyOn(input.element, "click")
        .mockImplementation(() => {});
      const uploadButton = wrapper.get(`button[aria-label="Upload ${label}"]`);
      expect(uploadButton.text()).toBe("Upload");
      expect(uploadButton.attributes("data-el-button")).toBe("true");
      await uploadButton.trigger("click");
      expect(openPicker).toHaveBeenCalledOnce();
      expect(resetButton(asset).text()).toBe("Restore");
      expect(resetButton(asset).attributes("data-type")).toBe("danger");
      const file = new File(["image"], "logo.png", { type: "image/png" });
      mocks.uploadSiteBrandingAsset.mockResolvedValue({
        ...useSiteBrandingStore(pinia).branding,
        [asset]: image,
      });
      await chooseFile(asset, file);
      expect(mocks.uploadSiteBrandingAsset).toHaveBeenLastCalledWith(
        "admin-token",
        asset,
        file,
        ...(asset === "header_logo" ? [true] : []),
      );
      expect(
        wrapper
          .get(
            `img[alt="${asset === "favicon" ? "Favicon" : "Header logo"} preview"]`,
          )
          .attributes("src"),
      ).toBe(image);
      mocks.resetSiteBrandingAsset.mockResolvedValue({
        ...useSiteBrandingStore(pinia).branding,
        [asset]: null,
      });
      await resetButton(asset).trigger("click");
      await flushPromises();
      expect(mocks.resetSiteBrandingAsset).toHaveBeenLastCalledWith(
        "admin-token",
        asset,
      );
      expect(useSiteBrandingStore(pinia).branding[asset]).toBeNull();
      expect(wrapper.get("#site-name").element.value).toBe("Unsaved name");
    }
    expect(mocks.updateSiteBranding).not.toHaveBeenCalled();
  });

  it("accepts SVG uploads independently for both image controls", async () => {
    mountPage();
    await flushPromises();
    for (const asset of ["header_logo", "favicon"]) {
      expect(wrapper.get(`#${asset}`).attributes("accept")).toContain(".svg");
      expect(wrapper.get(`#${asset}`).attributes("accept")).toContain(
        "image/svg+xml",
      );
      const file = new File([svgText], "logo.svg", { type: "image/svg+xml" });
      mocks.uploadSiteBrandingAsset.mockResolvedValue({
        ...useSiteBrandingStore(pinia).branding,
        [asset]: svgImage,
      });
      await chooseFile(asset, file);
      expect(mocks.uploadSiteBrandingAsset).toHaveBeenLastCalledWith(
        "admin-token",
        asset,
        file,
        ...(asset === "header_logo" ? [true] : []),
      );
      expect(useSiteBrandingStore(pinia).branding[asset]).toBe(svgImage);
    }
    expect(document.querySelector('link[rel="icon"]').type).toBe(
      "image/svg+xml",
    );
  });

  it("uploads header GIFs with playback preferences and favicon GIFs as static images", async () => {
    mountPage();
    await flushPromises();
    expect(playbackButton("header_logo")).toBeUndefined();
    expect(wrapper.find("#favicon-playback").exists()).toBe(false);
    await wrapper.get("#header_logo-playback").setValue("false");
    const headerFile = new File(["GIF"], "logo.gif", { type: "image/gif" });
    mocks.uploadSiteBrandingAsset.mockResolvedValue({
      ...original,
      header_logo: gifOnce,
    });
    await chooseFile("header_logo", headerFile);
    expect(mocks.uploadSiteBrandingAsset).toHaveBeenLastCalledWith(
      "admin-token",
      "header_logo",
      headerFile,
      false,
    );
    expect(wrapper.get("#header_logo-playback").element.value).toBe("false");
    expect(playbackButton("header_logo").element.disabled).toBe(true);
    expect(playbackButton("header_logo").text()).toBe("Save");
    expect(playbackButton("header_logo").attributes("data-type")).toBe(
      "primary",
    );

    await wrapper.get("#header_logo-playback").setValue("true");
    const faviconFile = new File(["GIF"], "favicon.gif", { type: "image/gif" });
    mocks.uploadSiteBrandingAsset.mockResolvedValue({
      ...original,
      header_logo: gifOnce,
      favicon: image,
    });
    await chooseFile("favicon", faviconFile);
    expect(mocks.uploadSiteBrandingAsset).toHaveBeenLastCalledWith(
      "admin-token",
      "favicon",
      faviconFile,
    );
    expect(useSiteBrandingStore(pinia).branding.favicon).toBe(image);
    expect(wrapper.get("#header_logo-playback").element.value).toBe("true");
    expect(playbackButton("header_logo").element.disabled).toBe(false);
    expect(document.querySelector('link[rel="icon"]').type).toBe("image/png");
    expect(wrapper.find("#favicon-playback").exists()).toBe(false);
    expect(playbackButton("favicon")).toBeUndefined();
    expect(wrapper.text()).toContain(
      "For favicons, only the first GIF frame is used.",
    );
  });

  it("omits favicon playback even for a legacy GIF while saving header playback without overwriting drafts", async () => {
    mocks.getSiteBranding.mockResolvedValue({
      ...original,
      header_logo: gifOnce,
      favicon: gifLoop,
    });
    mountPage();
    await flushPromises();
    expect(wrapper.get("#header_logo-playback").element.value).toBe("false");
    expect(wrapper.find("#favicon-playback").exists()).toBe(false);
    expect(playbackButton("favicon")).toBeUndefined();
    expect(wrapper.findAll("select")).toHaveLength(1);
    await wrapper.get("#site-name").setValue("Unsaved text");
    await wrapper.get("#header_logo-playback").setValue("true");
    mocks.updateSiteBrandingAssetAnimation.mockResolvedValue({
      ...original,
      header_logo: gifLoop,
      favicon: image,
    });
    await playbackButton("header_logo").trigger("click");
    await flushPromises();
    expect(
      mocks.updateSiteBrandingAssetAnimation,
    ).toHaveBeenCalledExactlyOnceWith("admin-token", "header_logo", true);
    expect(wrapper.get("#site-name").element.value).toBe("Unsaved text");
    expect(JSON.parse(localStorage.getItem(CACHE_KEY)).branding.favicon).toBe(
      image,
    );
    expect(document.querySelector('link[rel="icon"]').type).toBe("image/png");
  });

  it("keeps saved GIF and playback draft on failed playback save", async () => {
    mocks.getSiteBranding.mockResolvedValue({
      ...original,
      header_logo: gifLoop,
    });
    mountPage();
    await flushPromises();
    await wrapper.get("#header_logo-playback").setValue("false");
    let rejectSave;
    mocks.updateSiteBrandingAssetAnimation.mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectSave = reject;
        }),
    );
    await playbackButton("header_logo").trigger("click");
    expect(wrapper.get("#favicon").element.disabled).toBe(true);
    rejectSave(new Error("Playback save failed"));
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Playback save failed");
    expect(useSiteBrandingStore(pinia).branding.header_logo).toBe(gifLoop);
    expect(wrapper.get("#header_logo-playback").element.value).toBe("false");
    expect(playbackButton("header_logo").element.disabled).toBe(false);
    expect(success).not.toHaveBeenCalled();
  });

  it("restoring a default removes its GIF playback save control", async () => {
    mocks.getSiteBranding.mockResolvedValue({
      ...original,
      header_logo: gifOnce,
    });
    mountPage();
    await flushPromises();
    await resetButton("header_logo").trigger("click");
    await flushPromises();
    expect(playbackButton("header_logo")).toBeUndefined();
    expect(wrapper.get("#header_logo-playback").element.value).toBe("true");
  });

  it("rejects unsupported and oversized files before making a request", async () => {
    mountPage();
    await flushPromises();
    await chooseFile(
      "header_logo",
      new File(["<html/>"], "image.html", { type: "text/html" }),
    );
    expect(wrapper.get('[role="alert"]').text()).toContain(
      "Choose an SVG, PNG",
    );
    await chooseFile(
      "favicon",
      new File([new Uint8Array(2 * 1024 * 1024 + 1)], "huge.png", {
        type: "image/png",
      }),
    );
    expect(wrapper.get('[role="alert"]').text()).toContain("2 MiB");
    expect(mocks.uploadSiteBrandingAsset).not.toHaveBeenCalled();
  });

  it("prevents concurrent mutations and preserves the preview on upload failure", async () => {
    mountPage();
    await flushPromises();
    let rejectUpload;
    mocks.uploadSiteBrandingAsset.mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectUpload = reject;
        }),
    );
    await chooseFile("header_logo", new File(["image"], "logo.png"));
    expect(wrapper.get("#favicon").element.disabled).toBe(true);
    await wrapper.get("form").trigger("submit");
    expect(mocks.updateSiteBranding).not.toHaveBeenCalled();
    rejectUpload(new Error("Upload failed"));
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Upload failed");
    expect(useSiteBrandingStore(pinia).branding.header_logo).toBeNull();
    expect(success).not.toHaveBeenCalled();
  });

  it.each([401, 403])(
    "logs out and redirects on authentication error %s",
    async (status) => {
      mocks.getSiteBranding.mockRejectedValue({ response: { status } });
      mountPage();
      await flushPromises();
      expect(mocks.adminStore.logout).toHaveBeenCalledOnce();
      expect(mocks.router.push).toHaveBeenCalledWith("/login");
      expect(wrapper.get("fieldset").element.disabled).toBe(true);
    },
  );

  it("redirects without reading when there is no admin token", async () => {
    mocks.adminStore.token = "";
    mountPage();
    await flushPromises();
    expect(mocks.router.push).toHaveBeenCalledWith("/login");
    expect(mocks.getSiteBranding).not.toHaveBeenCalled();
  });

  it("keeps the custom image when restoring the default fails", async () => {
    mocks.getSiteBranding.mockResolvedValue({ ...original, favicon: image });
    mocks.resetSiteBrandingAsset.mockRejectedValue(new Error("Reset failed"));
    mountPage();
    await flushPromises();
    await resetButton("favicon").trigger("click");
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Reset failed");
    expect(useSiteBrandingStore(pinia).branding.favicon).toBe(image);
    expect(success).not.toHaveBeenCalled();
  });

  it.each(["text", "upload", "reset", "playback"])(
    "disables editing and redirects if auth expires during %s",
    async (operation) => {
      mocks.getSiteBranding.mockResolvedValue({
        ...original,
        favicon: image,
        header_logo: operation === "playback" ? gifLoop : null,
      });
      const unauthorized = { response: { status: 401 } };
      mountPage();
      await flushPromises();
      if (operation === "text") {
        mocks.updateSiteBranding.mockRejectedValue(unauthorized);
        await wrapper.get("form").trigger("submit");
      } else if (operation === "upload") {
        mocks.uploadSiteBrandingAsset.mockRejectedValue(unauthorized);
        await chooseFile("favicon", new File(["image"], "image.png"));
      } else if (operation === "playback") {
        mocks.updateSiteBrandingAssetAnimation.mockRejectedValue(unauthorized);
        await wrapper.get("#header_logo-playback").setValue("false");
        await playbackButton("header_logo").trigger("click");
      } else {
        mocks.resetSiteBrandingAsset.mockRejectedValue(unauthorized);
        await resetButton("favicon").trigger("click");
      }
      await flushPromises();
      expect(mocks.adminStore.logout).toHaveBeenCalledOnce();
      expect(mocks.router.push).toHaveBeenCalledWith("/login");
      expect(wrapper.get("fieldset").element.disabled).toBe(true);
      expect(success).not.toHaveBeenCalled();
    },
  );
});
