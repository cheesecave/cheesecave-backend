import { defineComponent, h } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn() },
  adminStore: { token: "admin-token", logout: vi.fn() },
  error: vi.fn(),
}));

vi.mock("vue-router", () => ({ useRouter: () => mocks.router }));
vi.mock("@/stores/admin", () => ({ useAdminStore: () => mocks.adminStore }));
vi.mock("@/utils/api", () => ({
  listS3Buckets: vi.fn(),
  listS3Objects: vi.fn().mockResolvedValue({ objects: [] }),
  deleteS3Object: vi.fn(),
  prepareDeleteS3Prefix: vi.fn(),
  deleteS3Prefix: vi.fn(),
  formatBytes: (bytes) => `${bytes} B`,
}));

function stub(name, testid) {
  return {
    default: defineComponent({
      name,
      props: { token: { type: String, default: "" } },
      emits: ["error"],
      setup(props) {
        return () =>
          h("section", { "data-testid": testid, "data-token": props.token });
      },
    }),
  };
}

vi.mock("@/components/AdminLayout.vue", () => ({
  default: defineComponent({
    setup(_, { slots }) {
      return () => h("main", slots.default ? slots.default() : []);
    },
  }),
}));
vi.mock("@/components/storage/OrphanLakefsRepos.vue", () =>
  stub("OrphanLakefsRepos", "orphans"),
);
vi.mock("@/components/storage/LfsReconciliation.vue", () =>
  stub("LfsReconciliation", "lfs"),
);
vi.mock("@/components/storage/UsageRecount.vue", () =>
  stub("UsageRecount", "usage"),
);

import StoragePage from "@/pages/storage.vue";

describe("admin storage page", () => {
  beforeEach(async () => {
    mocks.error.mockReset();
    // See test_cache_page.test.js: element-plus is spied on, not mocked.
    const elementPlus = await vi.importActual("element-plus");
    vi.spyOn(elementPlus.ElMessage, "error").mockImplementation(mocks.error);
  });

  it("shows the usage recount and reports its failures", async () => {
    const wrapper = mount(StoragePage, { global: { stubs: ElementPlusStubs } });
    await flushPromises();

    const recount = wrapper.findComponent({ name: "UsageRecount" });
    expect(recount.attributes("data-token")).toBe("admin-token");
    recount.vm.$emit("error", {
      response: { data: { detail: { error: "no worker" } } },
    });
    expect(mocks.error).toHaveBeenLastCalledWith("no worker");
    recount.vm.$emit("error", new Error("offline"));
    expect(mocks.error).toHaveBeenLastCalledWith(
      "Storage usage recount failed",
    );
  });
});
