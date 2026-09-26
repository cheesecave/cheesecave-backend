import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const api = vi.hoisted(() => ({
  listOrphanLakefsRepositories: vi.fn(),
  purgeOrphanLakefsRepository: vi.fn(),
}));
const dialogs = vi.hoisted(() => ({
  confirm: vi.fn(),
  success: vi.fn(),
}));

vi.mock("@/utils/api", () => ({
  listOrphanLakefsRepositories: (...args) =>
    api.listOrphanLakefsRepositories(...args),
  purgeOrphanLakefsRepository: (...args) =>
    api.purgeOrphanLakefsRepository(...args),
}));

import OrphanLakefsRepos from "@/components/storage/OrphanLakefsRepos.vue";

function orphan(id, overrides = {}) {
  return {
    id,
    created_at: 1790000000,
    storage_namespace: `s3://hub-storage/${id}`,
    purge_pending: false,
    ...overrides,
  };
}

function mountAudit() {
  return mount(OrphanLakefsRepos, {
    props: { token: "admin-token" },
    global: {
      stubs: {
        ...ElementPlusStubs,
        RouterLink: { template: "<a><slot /></a>" },
      },
    },
  });
}

describe("OrphanLakefsRepos", () => {
  beforeEach(async () => {
    Object.values(api).forEach((fn) => fn.mockReset());
    Object.values(dialogs).forEach((fn) => fn.mockReset());
    // See test_cache_page.test.js: element-plus is spied on, not mocked.
    const elementPlus = await vi.importActual("element-plus");
    vi.spyOn(elementPlus.ElMessageBox, "confirm").mockImplementation(
      (...args) => dialogs.confirm(...args),
    );
    vi.spyOn(elementPlus.ElMessage, "success").mockImplementation((...args) =>
      dialogs.success(...args),
    );
  });

  it("asks for a scan, then lists orphans with their status", async () => {
    api.listOrphanLakefsRepositories.mockResolvedValue({
      orphans: [
        orphan("m-old-model-a"),
        orphan("d-old-data-b", { purge_pending: true, created_at: null }),
      ],
      count: 2,
    });
    const wrapper = mountAudit();
    expect(wrapper.find('[data-testid="orphan-not-scanned"]').exists()).toBe(
      true,
    );
    expect(wrapper.get('[data-testid="orphan-scan"]').text()).toBe("Scan");

    await wrapper.get('[data-testid="orphan-scan"]').trigger("click");
    await flushPromises();

    expect(api.listOrphanLakefsRepositories).toHaveBeenCalledWith(
      "admin-token",
    );
    const rows = wrapper.findAll("tr");
    expect(rows[0].text()).toContain("m-old-model-a");
    expect(rows[0].text()).toContain("s3://hub-storage/m-old-model-a");
    expect(rows[0].text()).toContain("orphaned");
    expect(rows[0].text()).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/);
    expect(rows[1].text()).toContain("purge scheduled");
    expect(rows[1].text()).toContain("—");
    expect(
      wrapper
        .get('[data-testid="orphan-purge-d-old-data-b"]')
        .attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.get('[data-testid="orphan-scan"]').text()).toBe("Rescan");
    // Only one orphan can still be purged, so there is no "purge all".
    expect(wrapper.find('[data-testid="orphan-purge-all"]').exists()).toBe(
      false,
    );
  });

  it("says when there is nothing to clean", async () => {
    api.listOrphanLakefsRepositories.mockResolvedValue({
      orphans: [],
      count: 0,
    });
    const wrapper = mountAudit();
    await wrapper.get('[data-testid="orphan-scan"]').trigger("click");
    await flushPromises();

    expect(wrapper.find('[data-testid="orphan-none"]').exists()).toBe(true);
  });

  it("purges one orphan after confirmation and marks it scheduled", async () => {
    api.listOrphanLakefsRepositories.mockResolvedValue({
      orphans: [orphan("m-a"), orphan("m-b")],
    });
    api.purgeOrphanLakefsRepository.mockResolvedValue({ task_id: 7 });
    dialogs.confirm.mockRejectedValueOnce("cancel").mockResolvedValue("ok");
    const wrapper = mountAudit();
    await wrapper.get('[data-testid="orphan-scan"]').trigger("click");
    await flushPromises();

    await wrapper.get('[data-testid="orphan-purge-m-a"]').trigger("click");
    await flushPromises();
    expect(api.purgeOrphanLakefsRepository).not.toHaveBeenCalled();

    await wrapper.get('[data-testid="orphan-purge-m-a"]').trigger("click");
    await flushPromises();
    expect(dialogs.confirm.mock.calls[1][0]).toContain("s3://hub-storage/m-a");
    expect(api.purgeOrphanLakefsRepository).toHaveBeenCalledWith(
      "admin-token",
      "m-a",
    );
    expect(dialogs.success).toHaveBeenCalledWith("Purge of m-a scheduled");
    expect(wrapper.findAll("tr")[0].text()).toContain("purge scheduled");
  });

  it("purges every remaining orphan and reports failures", async () => {
    api.listOrphanLakefsRepositories.mockResolvedValue({
      orphans: [orphan("m-a"), orphan("m-b"), orphan("m-c")],
    });
    api.purgeOrphanLakefsRepository
      .mockResolvedValueOnce({ task_id: 1 })
      .mockRejectedValueOnce(new Error("conflict"))
      .mockResolvedValueOnce({ task_id: 3 });
    dialogs.confirm.mockRejectedValueOnce("cancel").mockResolvedValue("ok");
    const wrapper = mountAudit();
    await wrapper.get('[data-testid="orphan-scan"]').trigger("click");
    await flushPromises();

    await wrapper.get('[data-testid="orphan-purge-all"]').trigger("click");
    await flushPromises();
    expect(api.purgeOrphanLakefsRepository).not.toHaveBeenCalled();

    expect(wrapper.get('[data-testid="orphan-purge-all"]').text()).toContain(
      "(3)",
    );
    await wrapper.get('[data-testid="orphan-purge-all"]').trigger("click");
    await flushPromises();

    expect(api.purgeOrphanLakefsRepository).toHaveBeenCalledTimes(3);
    expect(dialogs.success).toHaveBeenCalledWith("Scheduled 2 purge(s)");
    expect(wrapper.emitted("error")[0][0].message).toBe("conflict");
  });

  it("reports a failed scan", async () => {
    api.listOrphanLakefsRepositories.mockRejectedValue(
      new Error("lakefs down"),
    );
    const wrapper = mountAudit();
    await wrapper.get('[data-testid="orphan-scan"]').trigger("click");
    await flushPromises();

    expect(wrapper.emitted("error")[0][0].message).toBe("lakefs down");
    expect(wrapper.find('[data-testid="orphan-not-scanned"]').exists()).toBe(
      true,
    );
  });
});
