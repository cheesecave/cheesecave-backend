import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import { createPinia, setActivePinia } from "pinia";
import { useSiteBrandingStore } from "@/stores/siteBranding";

import TheFooter from "@/components/layout/TheFooter.vue";

describe("TheFooter", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("renders primary navigation and community links", () => {
    const wrapper = mount(TheFooter);
    const hrefs = wrapper.findAll("a").map((link) => link.attributes("href"));

    expect(wrapper.text()).toContain("Self-hosted HuggingFace Hub alternative");
    expect(hrefs).toEqual(
      expect.arrayContaining([
        "/docs",
        "/about",
        "/get-started",
        "/self-hosted",
        "/terms",
        "/privacy",
        "https://github.com/KohakuBlueleaf/KohakuHub",
        "https://discord.gg/xWYrkyvJ2s",
      ]),
    );
  });

  it("updates only the site introduction and preserves upstream attribution", async () => {
    const store = useSiteBrandingStore();
    const wrapper = mount(TheFooter);
    store.apply({
      ...store.branding,
      site_name: "DeepGHS Hub",
      footer_description: "A community model hub.\nModels and datasets.",
    });
    await wrapper.vm.$nextTick();

    expect(wrapper.find("h3").text()).toBe("DeepGHS Hub");
    expect(wrapper.get(".whitespace-pre-wrap").text()).toBe(
      "A community model hub.\nModels and datasets.",
    );
    expect(wrapper.text()).toContain(
      "© 2025 KohakuHub. Licensed under AGPL-3.0",
    );
    expect(
      wrapper.find('a[href="https://discord.gg/xWYrkyvJ2s"]').exists(),
    ).toBe(true);
  });
});
