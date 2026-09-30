import { defineComponent, h } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn() },
  adminStore: { token: "admin-token", logout: vi.fn() },
  api: {
    listRepositories: vi.fn(),
    getRepositoryDetails: vi.fn(),
    getRepositoryStorageBreakdown: vi.fn(),
    recalculateAllRepoStorage: vi.fn(),
    listCommits: vi.fn(),
    deleteRepositoryAdmin: vi.fn(),
    moveRepositoryAdmin: vi.fn(),
    squashRepositoryAdmin: vi.fn(),
    getSiteConfig: vi.fn(),
  },
}));

vi.mock("vue-router", () => ({
  useRouter: () => mocks.router,
}));

vi.mock("@/stores/admin", () => ({
  useAdminStore: () => mocks.adminStore,
}));

vi.mock("@/utils/api", () => ({
  ...mocks.api,
  formatBytes: (value) => String(value ?? 0),
}));

vi.mock("@/components/AdminLayout.vue", () => ({
  default: defineComponent({
    setup(_, { slots }) {
      return () => h("main", slots.default ? slots.default() : []);
    },
  }),
}));

vi.mock("@/components/FileTree.vue", () => ({
  default: defineComponent({
    setup() {
      return () => h("div");
    },
  }),
}));

import RepositoriesPage from "@/pages/repositories.vue";

function mountPage() {
  return mount(RepositoriesPage, {
    global: { stubs: ElementPlusStubs },
  });
}

function enabledConfig(squash) {
  return {
    capabilities: {
      repository_operations: { revert: false, reset: false, squash },
    },
  };
}

describe("admin repository operation capability consumer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.adminStore.token = "admin-token";
    mocks.api.listRepositories.mockResolvedValue({
      repositories: [
        {
          id: 1,
          repo_type: "model",
          namespace: "owner",
          name: "demo",
          full_id: "owner/demo",
          owner_username: "owner",
          used_bytes: 0,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      total: 1,
    });
    mocks.api.getRepositoryDetails.mockResolvedValue({
      repo_type: "model",
      namespace: "owner",
      name: "demo",
      full_id: "owner/demo",
    });
    mocks.api.getSiteConfig.mockResolvedValue(enabledConfig(false));
  });

  async function openRepositoryDetails() {
    const wrapper = mountPage();
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "View Details")
      .trigger("click");
    await flushPromises();
    return wrapper;
  }

  it("hides squash when capability loading fails", async () => {
    mocks.api.getSiteConfig.mockRejectedValueOnce(new Error("offline"));

    const wrapper = await openRepositoryDetails();

    expect(wrapper.text()).not.toContain("Squash Repository");
    expect(mocks.api.squashRepositoryAdmin).not.toHaveBeenCalled();
  });

  it("shows squash only for an explicit true capability", async () => {
    mocks.api.getSiteConfig.mockResolvedValueOnce(enabledConfig(true));

    const wrapper = await openRepositoryDetails();

    expect(wrapper.text()).toContain("Squash Repository");
  });
});

describe("admin storage recount of repositories", () => {
  const messages = { success: vi.fn(), error: vi.fn(), confirm: vi.fn() };

  beforeEach(async () => {
    vi.clearAllMocks();
    mocks.adminStore.token = "admin-token";
    mocks.api.listRepositories.mockResolvedValue({
      repositories: [],
      total: 0,
    });
    mocks.api.getSiteConfig.mockResolvedValue(enabledConfig(false));
    // See test_cache_page.test.js: element-plus is spied on, not mocked.
    const elementPlus = await vi.importActual("element-plus");
    vi.spyOn(elementPlus.ElMessage, "success").mockImplementation(
      messages.success,
    );
    vi.spyOn(elementPlus.ElMessage, "error").mockImplementation(messages.error);
    vi.spyOn(elementPlus.ElMessageBox, "confirm").mockImplementation(
      messages.confirm,
    );
  });

  async function recount(namespace) {
    const wrapper = mountPage();
    await flushPromises();
    if (namespace) {
      await wrapper
        .get('input[placeholder="Filter by namespace"]')
        .setValue(namespace);
    }
    await wrapper
      .findAll("button")
      .find((button) => button.text().includes("Recount All Storage"))
      .trigger("click");
    await flushPromises();
    return wrapper;
  }

  it("schedules a background recount and says so", async () => {
    messages.confirm.mockResolvedValue("confirm");
    mocks.api.recalculateAllRepoStorage
      .mockResolvedValueOnce({ task_id: 12, already_pending: false })
      .mockResolvedValueOnce({ task_id: null, already_pending: true });

    await recount();
    expect(messages.confirm.mock.calls[0][0]).toContain(
      "every repository in the background",
    );
    expect(mocks.api.recalculateAllRepoStorage).toHaveBeenCalledWith(
      "admin-token",
      {
        namespace: undefined,
      },
    );
    expect(messages.success).toHaveBeenLastCalledWith(
      "Recount scheduled (task #12); follow it under Background Tasks",
    );

    await recount("aurora-labs");
    expect(messages.confirm.mock.calls[1][0]).toContain(
      "every repository of aurora-labs",
    );
    expect(mocks.api.recalculateAllRepoStorage).toHaveBeenLastCalledWith(
      "admin-token",
      {
        namespace: "aurora-labs",
      },
    );
    expect(messages.success).toHaveBeenLastCalledWith(
      "A recount is already scheduled",
    );
  });

  it("does nothing when cancelled and reports a failure", async () => {
    messages.confirm.mockRejectedValueOnce("cancel");
    await recount();
    expect(mocks.api.recalculateAllRepoStorage).not.toHaveBeenCalled();
    expect(messages.error).not.toHaveBeenCalled();

    messages.confirm.mockResolvedValue("confirm");
    mocks.api.recalculateAllRepoStorage.mockRejectedValueOnce({
      response: { data: { detail: { error: "no worker" } } },
    });
    await recount();
    expect(messages.error).toHaveBeenLastCalledWith("no worker");
    mocks.api.recalculateAllRepoStorage.mockRejectedValueOnce(
      new Error("offline"),
    );
    await recount();
    expect(messages.error).toHaveBeenLastCalledWith(
      "Failed to recount repository storage",
    );
  });
});
