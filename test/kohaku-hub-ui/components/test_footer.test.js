import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import TheFooter from "@/components/layout/TheFooter.vue";

describe("TheFooter", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([false, true])("shows the frontend commit with dirty=%s", (dirty) => {
    const commit = "0123456789abcdef0123456789abcdef01234567";
    vi.stubGlobal("__BUILD_INFO__", { commit, dirty });
    const version = mount(TheFooter).get('[data-testid="frontend-version"]');

    expect(version.text()).toBe(`Frontend 0123456${dirty ? "-dirty" : ""}`);
    expect(version.attributes("title")).toContain(commit);
    expect(version.attributes("title").includes("uncommitted changes")).toBe(
      dirty,
    );
    const link = version.get("a");
    expect(link.attributes("href")).toBe(
      `https://github.com/deepghs/KohakuHub/commit/${commit}`,
    );
    expect(link.attributes("target")).toBe("_blank");
    expect(link.attributes("rel")).toBe("noopener noreferrer");
  });

  it("shows an explicit unknown version when build metadata is unavailable", () => {
    vi.stubGlobal("__BUILD_INFO__", { commit: "unknown", dirty: false });
    const version = mount(TheFooter).get('[data-testid="frontend-version"]');

    expect(version.text()).toBe("Frontend unknown");
    expect(version.attributes("title")).toBe("Frontend Git commit unavailable");
    expect(version.find("a").exists()).toBe(false);
  });

  it("preserves the original footer and appends fork, upstream, and license information", () => {
    const wrapper = mount(TheFooter);

    expect(wrapper.findAll("h3").map((heading) => heading.text())).toEqual([
      "KohakuHub",
      "Resources",
      "Start",
      "Community",
      "Legal",
    ]);
    const copyright = wrapper.get(".text-center");
    expect(copyright.text()).toContain(
      "© 2025 KohakuHub. Licensed under AGPL-3.0",
    );
    expect(copyright.classes()).toContain("pt-8");
    expect(copyright.classes()).not.toContain("flex");
    expect(wrapper.text()).toContain("Based on KohakuHub");
    expect(wrapper.text()).toContain("Powered by DeepGHS · Based on KohakuHub");
    expect(wrapper.text()).not.toContain("Open source under");
    const lines = copyright.findAll("p");
    expect(lines.map((line) => line.text())).toEqual([
      "Powered by DeepGHS · Based on KohakuHub",
      "© 2025 KohakuHub. Licensed under AGPL-3.0",
    ]);
    const originalLine = lines[1];
    expect(originalLine.text()).toBe(
      "© 2025 KohakuHub. Licensed under AGPL-3.0",
    );
    expect(
      originalLine
        .get('a[href="https://github.com/KohakuBlueleaf/KohakuHub"]')
        .text(),
    ).toBe("KohakuHub");
    expect(
      copyright.get('a[href="https://github.com/deepghs/KohakuHub"]').text(),
    ).toBe("DeepGHS");
    expect(
      originalLine
        .get('a[href="https://github.com/deepghs/KohakuHub/blob/main/LICENSE"]')
        .text(),
    ).toBe("AGPL-3.0");
    expect(
      wrapper.find('a[href="https://discord.gg/xWYrkyvJ2s"]').exists(),
    ).toBe(true);
  });

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
        "https://github.com/deepghs/KohakuHub",
        "https://github.com/deepghs/KohakuHub/issues",
      ]),
    );
  });
});
