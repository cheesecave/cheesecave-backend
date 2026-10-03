import { mount } from "@vue/test-utils";
import { h } from "vue";
import { describe, expect, it, vi } from "vitest";
import AdminPage from "@/components/AdminPage.vue";
import AdminPageHeader from "@/components/AdminPageHeader.vue";

describe("admin page header", () => {
  it("renders text title and subtitle without an empty actions area", () => {
    const wrapper = mount(AdminPageHeader, {
      props: { title: "Users", subtitle: "Manage accounts." },
    });

    expect(wrapper.get("h1").text()).toBe("Users");
    expect(wrapper.get(".admin-page-subtitle").text()).toBe("Manage accounts.");
    expect(wrapper.find(".admin-page-actions").exists()).toBe(false);
  });

  it("preserves rich titles, subtitles and interactive header actions", async () => {
    const openWorkers = vi.fn();
    const refresh = vi.fn();
    const wrapper = mount(AdminPageHeader, {
      props: { title: "Fallback title" },
      slots: {
        title: () => [
          h("span", "Background Tasks"),
          h("button", { onClick: openWorkers }, "2 workers online"),
        ],
        subtitle: "Tasks executed by <code>khub-worker</code>.",
        actions: () => h("button", { onClick: refresh }, "Refresh"),
      },
    });

    expect(wrapper.get("h1").text()).toContain("Background Tasks");
    expect(wrapper.text()).not.toContain("Fallback title");
    expect(wrapper.get(".admin-page-subtitle code").text()).toBe("khub-worker");
    await wrapper.get("h1 button").trigger("click");
    await wrapper.get(".admin-page-actions button").trigger("click");
    expect(openWorkers).toHaveBeenCalledOnce();
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("keeps page content and its actions outside the header", () => {
    const wrapper = mount(AdminPage, {
      slots: {
        default:
          '<AdminPageHeader title="Storage" subtitle="Browse objects." /><section><button>Up</button></section>',
      },
      global: { components: { AdminPageHeader } },
    });

    expect(wrapper.get(".admin-page").exists()).toBe(true);
    expect(wrapper.get("section button").text()).toBe("Up");
    expect(wrapper.get("header").find("button").exists()).toBe(false);
  });
});
