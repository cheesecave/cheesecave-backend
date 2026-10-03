import { beforeEach, describe, expect, it, vi } from "vitest";
import axios from "@/testing/axios";
import {
  getSiteBranding,
  updateSiteBranding,
  uploadSiteBrandingAsset,
  updateSiteBrandingAssetAnimation,
  resetSiteBrandingAsset,
} from "@/utils/api";

describe("admin site branding API", () => {
  const branding = {
    site_name: "Hub",
    footer_description: "",
    header_logo: null,
    favicon: null,
  };
  const client = {
    get: vi.fn(),
    put: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  };
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(axios, "create").mockReturnValue(client);
    Object.values(client).forEach((method) =>
      method.mockResolvedValue({ data: branding }),
    );
  });

  it("sends authenticated reads and text updates and returns server branding", async () => {
    expect(await getSiteBranding("admin-token")).toEqual(branding);
    expect(client.get).toHaveBeenCalledWith("/site-branding", {
      timeout: 30000,
    });
    const patch = { site_name: "Hub", footer_description: "" };
    expect(await updateSiteBranding("admin-token", patch)).toEqual(branding);
    expect(client.put).toHaveBeenCalledWith("/site-branding", patch, {
      timeout: 30000,
    });
    expect(axios.create).toHaveBeenCalledWith({
      baseURL: "/admin/api",
      headers: { "X-Admin-Token": "admin-token" },
    });
  });

  it.each(["header_logo", "favicon"])(
    "uploads and restores %s using separate endpoints",
    async (asset) => {
      const file = new File(["data"], "image.png", { type: "image/png" });
      expect(await uploadSiteBrandingAsset("admin-token", asset, file)).toEqual(
        branding,
      );
      const [url, form, options] = client.post.mock.calls[0];
      expect(url).toBe(`/site-branding/assets/${asset}`);
      expect(form).toBeInstanceOf(FormData);
      expect(form.get("file")).toBe(file);
      expect(form.get("loop")).toBe(asset === "header_logo" ? "true" : null);
      expect(options).toEqual({ timeout: 30000 });
      expect(await resetSiteBrandingAsset("admin-token", asset)).toEqual(
        branding,
      );
      expect(client.delete).toHaveBeenCalledWith(
        `/site-branding/assets/${asset}`,
        { timeout: 30000 },
      );
    },
  );

  it("sends the selected GIF playback setting in upload and later updates", async () => {
    const file = new File(["GIF"], "logo.gif", { type: "image/gif" });
    await uploadSiteBrandingAsset("admin-token", "header_logo", file, false);
    expect(client.post.mock.calls[0][1].get("loop")).toBe("false");
    expect(
      await updateSiteBrandingAssetAnimation(
        "admin-token",
        "header_logo",
        false,
      ),
    ).toEqual(branding);
    expect(client.patch).toHaveBeenLastCalledWith(
      "/site-branding/assets/header_logo/animation",
      { loop: false },
      { timeout: 30000 },
    );
  });

  it("omits playback options for favicon uploads even when a caller supplies them", async () => {
    const file = new File(["GIF"], "favicon.gif", { type: "image/gif" });
    await uploadSiteBrandingAsset("admin-token", "favicon", file, false);
    expect(client.post.mock.calls[0][1].get("loop")).toBeNull();
  });
});
