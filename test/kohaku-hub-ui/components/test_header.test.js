import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";
import { flushPromises } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  router: {
    push: vi.fn(),
  },
  route: {
    params: {},
    query: {},
  },
  elMessage: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("vue-router/auto", () => ({
  useRouter: () => mocks.router,
  useRoute: () => mocks.route,
}));

vi.mock("element-plus", () => ({
  ElMessage: mocks.elMessage,
}));

import TheHeader from "@/components/layout/TheHeader.vue";
import { useAuthStore } from "@/stores/auth";
import { useThemeStore } from "@/stores/theme";
import { useSiteBrandingStore } from "@/stores/siteBranding";

describe("TheHeader", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    document.documentElement.className = "";
    setActivePinia(createPinia());
  });

  function mountHeader() {
    return mount(TheHeader, {
      global: {
        mocks: {
          $router: mocks.router,
        },
        stubs: {
          ...ElementPlusStubs,
          RouterLink: RouterLinkStub,
        },
      },
    });
  }

  it("renders visitor navigation and toggles theme", async () => {
    const themeStore = useThemeStore();
    const wrapper = mountHeader();

    expect(wrapper.text()).toContain("Models");
    expect(wrapper.text()).toContain("Datasets");
    expect(wrapper.text()).toContain("Login");
    expect(wrapper.text()).toContain("Sign Up");

    const buttons = wrapper.findAll("button");
    await buttons[0].trigger("click");

    expect(themeStore.isDark).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("theme")).toBe("dark");

    const signUpButton = buttons.find((button) =>
      button.text().includes("Sign Up"),
    );
    await signUpButton.trigger("click");

    expect(mocks.router.push).toHaveBeenCalledWith("/register");
  });

  it("renders authenticated actions and routes create/profile/logout flows", async () => {
    const authStore = useAuthStore();
    authStore.user = {
      username: "alice",
    };
    authStore.logout = vi.fn().mockResolvedValue(undefined);

    const wrapper = mountHeader();

    expect(wrapper.text()).toContain("alice");
    expect(wrapper.text()).toContain("New Model");
    expect(wrapper.text()).toContain("New Dataset");
    expect(wrapper.text()).toContain("New Space");
    expect(wrapper.text()).toContain("New Organization");

    const buttons = wrapper.findAll("button");

    await buttons
      .find((button) => button.text().includes("New Model"))
      .trigger("click");
    await buttons
      .find((button) => button.text().includes("New Organization"))
      .trigger("click");
    await buttons
      .find((button) => button.text().includes("Profile"))
      .trigger("click");
    await buttons
      .find((button) => button.text().includes("Settings"))
      .trigger("click");
    await buttons
      .find((button) => button.text().includes("Logout"))
      .trigger("click");

    expect(mocks.router.push).toHaveBeenCalledWith({
      path: "/new",
      query: { type: "model" },
    });
    expect(mocks.router.push).toHaveBeenCalledWith("/organizations/new");
    expect(mocks.router.push).toHaveBeenCalledWith("/alice");
    expect(mocks.router.push).toHaveBeenCalledWith("/settings");
    expect(authStore.logout).toHaveBeenCalled();
    expect(mocks.router.push).toHaveBeenCalledWith("/");
  });

  it("opens the mobile menu and falls back to the default avatar", async () => {
    const authStore = useAuthStore();
    authStore.user = {
      username: "alice",
    };
    authStore.logout = vi.fn().mockResolvedValue(undefined);

    const wrapper = mountHeader();

    const menuButton = wrapper
      .findAll("button")
      .find((button) => button.find(".i-carbon-menu").exists());
    await menuButton.trigger("click");
    await nextTick();

    expect(wrapper.find('[data-el-drawer="true"]').exists()).toBe(true);

    const avatar = wrapper.get('img[alt="alice avatar"]');
    await avatar.trigger("error");
    await nextTick();

    expect(wrapper.find(".i-carbon-user-avatar").exists()).toBe(true);
  });

  it("shows an error message when logout fails", async () => {
    const authStore = useAuthStore();
    authStore.user = {
      username: "alice",
    };
    authStore.logout = vi.fn().mockRejectedValue(new Error("network"));

    const wrapper = mountHeader();

    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Logout"))
      .trigger("click");
    await flushPromises();

    expect(authStore.logout).toHaveBeenCalled();
    expect(mocks.router.push).not.toHaveBeenCalledWith("/");
  });

  it("reacts to branding updates, exposes full names, and falls back on image errors", async () => {
    const store = useSiteBrandingStore();
    const name = "A long community hub name ".repeat(3);
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==";
    store.apply({ ...store.branding, site_name: name, header_logo: png });
    const wrapper = mountHeader();
    const home = wrapper
      .findAll("a")
      .find((link) => link.attributes("href") === "/");
    expect(home.attributes("aria-label")).toBe(name.trim());
    expect(home.attributes("title")).toBe(name.trim());
    expect(home.get("span").classes()).toContain("truncate");
    expect(home.get("img").attributes("src")).toBe(png);

    await home.get("img").trigger("error");
    expect(home.get("img").attributes("src")).toBe("/images/logo-square.svg");
    store.apply({
      ...store.branding,
      site_name: "Renamed Hub",
      header_logo: null,
    });
    await nextTick();
    expect(home.text()).toBe("Renamed Hub");
    expect(home.get("img").attributes("alt")).toBe("Renamed Hub");
  });
});
