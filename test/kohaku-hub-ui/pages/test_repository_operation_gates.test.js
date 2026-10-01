import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";
import axios from "@/testing/axios";

const mocks = vi.hoisted(() => ({
  route: {
    params: {
      type: "model",
      namespace: "owner",
      name: "demo",
      commit_id: "commit-1",
    },
  },
  router: {
    push: vi.fn(),
    back: vi.fn(),
  },
  authStore: { isAuthenticated: true },
  settingsAPI: {
    getSiteConfig: vi.fn(),
    revertBranch: vi.fn(),
    resetBranch: vi.fn(),
    squashRepo: vi.fn(),
    updateRepoSettings: vi.fn(),
    getLfsSettings: vi.fn(),
  },
  repoAPI: {
    getInfo: vi.fn(),
    delete: vi.fn(),
    getCommitOperations: vi.fn(),
    getCommitUnavailableFiles: vi.fn(),
  },
  validationAPI: { checkName: vi.fn() },
  quotaAPI: {
    getRepoQuota: vi.fn(),
    recalculateRepoStorage: vi.fn(),
    setRepoQuota: vi.fn(),
  },
}));

vi.mock("vue-router/auto", () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}));

vi.mock("vue-router", () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}));

vi.mock("@/stores/auth", () => ({
  useAuthStore: () => mocks.authStore,
}));

vi.mock("@/utils/api", () => ({
  settingsAPI: mocks.settingsAPI,
  repoAPI: mocks.repoAPI,
  validationAPI: mocks.validationAPI,
  quotaAPI: mocks.quotaAPI,
}));

import CommitPage from "@/pages/[type]s/[namespace]/[name]/commit/[commit_id].vue";
import SettingsPage from "@/pages/[type]s/[namespace]/[name]/settings.vue";

function mountPage(component) {
  return mount(component, {
    global: {
      stubs: {
        ...ElementPlusStubs,
        RouterLink: RouterLinkStub,
      },
    },
  });
}

function enabledConfig(overrides = {}) {
  return {
    capabilities: {
      repository_operations: {
        revert: false,
        reset: false,
        squash: false,
        ...overrides,
      },
    },
  };
}

describe("repository operation capability consumers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(axios, "get").mockImplementation((url) => {
      if (url.endsWith("/diff"))
        return Promise.resolve({ data: { files: [] } });
      return Promise.resolve({
        data: {
          commit_id: "commit-1",
          message: "Initial commit",
          author: "owner",
          date: 1_700_000_000,
          files: [],
        },
      });
    });
    mocks.authStore.isAuthenticated = true;
    mocks.settingsAPI.getSiteConfig.mockResolvedValue({
      data: enabledConfig(),
    });
    mocks.settingsAPI.revertBranch.mockResolvedValue({ data: {} });
    mocks.settingsAPI.resetBranch.mockResolvedValue({ data: {} });
    mocks.settingsAPI.squashRepo.mockResolvedValue({ data: {} });
    mocks.repoAPI.getInfo.mockResolvedValue({ data: { private: false } });
    mocks.repoAPI.getCommitOperations.mockResolvedValue({ data: {} });
    mocks.repoAPI.getCommitUnavailableFiles.mockResolvedValue({
      data: { files: [] },
    });
  });

  it("hides commit actions when capability loading fails", async () => {
    mocks.settingsAPI.getSiteConfig.mockRejectedValueOnce(new Error("offline"));

    const wrapper = mountPage(CommitPage);
    await flushPromises();

    expect(wrapper.text()).not.toContain("Revert Commit");
    expect(wrapper.text()).not.toContain("Reset to This State");
    expect(mocks.settingsAPI.revertBranch).not.toHaveBeenCalled();
    expect(mocks.settingsAPI.resetBranch).not.toHaveBeenCalled();
  });

  it("shows only explicitly enabled commit actions and routes through the API client", async () => {
    mocks.settingsAPI.getSiteConfig.mockResolvedValueOnce({
      data: enabledConfig({ revert: true }),
    });

    const wrapper = mountPage(CommitPage);
    await flushPromises();

    expect(wrapper.text()).toContain("Revert Commit");
    expect(wrapper.text()).not.toContain("Reset to This State");

    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Revert Commit")
      .trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("Revert");
    // LakeFS refuses a conflict with or without force: no such option
    expect(wrapper.text()).not.toContain("Force revert");
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Revert")
      .trigger("click");
    await flushPromises();

    expect(mocks.settingsAPI.revertBranch).toHaveBeenCalledWith(
      "model",
      "owner",
      "demo",
      "main",
      { ref: "commit-1", parent_number: 1, allow_empty: false },
    );
    expect(mocks.settingsAPI.resetBranch).not.toHaveBeenCalled();
  });

  it("hides squash in repository settings when the capability is false", async () => {
    const wrapper = mountPage(SettingsPage);
    await flushPromises();

    expect(wrapper.text()).not.toContain("Squash repository history");
    expect(wrapper.text()).not.toContain("Squash Repository");
    expect(mocks.settingsAPI.squashRepo).not.toHaveBeenCalled();
  });

  it("shows squash in repository settings only when explicitly enabled", async () => {
    mocks.settingsAPI.getSiteConfig.mockResolvedValueOnce({
      data: enabledConfig({ squash: true }),
    });

    const wrapper = mountPage(SettingsPage);
    await flushPromises();

    expect(wrapper.text()).toContain("Squash repository history");
    expect(wrapper.text()).toContain("Squash Repository");
  });

  it("squashes after two confirmations and says so", async () => {
    mocks.settingsAPI.getSiteConfig.mockResolvedValueOnce({
      data: enabledConfig({ squash: true }),
    });
    // See test_cache_page.test.js: element-plus is spied on, not mocked.
    const elementPlus = await vi.importActual("element-plus");
    const confirm = vi
      .spyOn(elementPlus.ElMessageBox, "confirm")
      .mockResolvedValue("confirm");
    vi.spyOn(elementPlus.ElMessageBox, "prompt").mockResolvedValue({
      value: "demo",
    });
    const success = vi
      .spyOn(elementPlus.ElMessage, "success")
      .mockImplementation(() => {});
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const wrapper = mountPage(SettingsPage);
      await flushPromises();
      await wrapper
        .findAll("button")
        .find((button) => button.text() === "Squash Repository")
        .trigger("click");
      await flushPromises();

      expect(confirm.mock.calls[0][0]).toContain(
        "old versions are removed in the background",
      );
      expect(mocks.settingsAPI.squashRepo).toHaveBeenCalledWith({
        repo: "owner/demo",
        type: "model",
      });
      expect(success).toHaveBeenCalledWith("Repository squashed successfully");
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});
