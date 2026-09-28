import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const api = vi.hoisted(() => ({
  getLfsReconciliation: vi.fn(),
  startLfsReconciliation: vi.fn(),
}));
const dialogs = vi.hoisted(() => ({ success: vi.fn() }));

vi.mock("@/utils/api", () => ({
  getLfsReconciliation: (...args) => api.getLfsReconciliation(...args),
  startLfsReconciliation: (...args) => api.startLfsReconciliation(...args),
}));

import LfsReconciliation from "@/components/storage/LfsReconciliation.vue";

function task(overrides = {}) {
  return {
    id: 7,
    status: "running",
    progress_done: 3,
    progress_total: 12,
    stage: "reconciling model:owner/demo",
    stats: { history_added: 4, files_fixed: 1 },
    created_at: "2026-09-27T10:00:00",
    finished_at: null,
    ...overrides,
  };
}

function mountPanel() {
  return mount(LfsReconciliation, {
    props: { token: "admin-token" },
    global: {
      stubs: {
        ...ElementPlusStubs,
        RouterLink: { template: "<a><slot /></a>" },
      },
    },
  });
}

describe("LfsReconciliation", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    Object.values(api).forEach((fn) => fn.mockReset());
    dialogs.success.mockReset();
    // See test_cache_page.test.js: element-plus is spied on, not mocked.
    const elementPlus = await vi.importActual("element-plus");
    vi.spyOn(elementPlus.ElMessage, "success").mockImplementation((...args) =>
      dialogs.success(...args),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("warns that collection waits while no reconciliation has completed", async () => {
    api.getLfsReconciliation.mockResolvedValue({
      reconciled_at: null,
      auto_gc: true,
      task: null,
    });
    const wrapper = mountPanel();
    await flushPromises();

    expect(api.getLfsReconciliation).toHaveBeenCalledWith("admin-token");
    expect(wrapper.get('[data-testid="lfs-reconciled-at"]').text()).toBe(
      "never",
    );
    expect(wrapper.get('[data-testid="lfs-auto-gc"]').text()).toBe("on");
    expect(
      wrapper.find('[data-testid="lfs-collection-waiting"]').exists(),
    ).toBe(true);
    expect(wrapper.find('[data-testid="lfs-reconcile-none"]').exists()).toBe(
      true,
    );
  });

  it("follows a running task until it finishes", async () => {
    api.getLfsReconciliation
      .mockResolvedValueOnce({
        reconciled_at: null,
        auto_gc: false,
        task: task(),
      })
      .mockResolvedValueOnce({
        reconciled_at: "2026-09-27T10:05:00",
        auto_gc: false,
        task: task({
          status: "succeeded",
          progress_done: 12,
          finished_at: "2026-09-27T10:05:00",
        }),
      });
    const wrapper = mountPanel();
    await flushPromises();

    expect(wrapper.get('[data-testid="lfs-auto-gc"]').text()).toBe("off");
    expect(
      wrapper.find('[data-testid="lfs-collection-waiting"]').exists(),
    ).toBe(false);
    expect(wrapper.get('[data-testid="lfs-reconcile-task"]').text()).toContain(
      "3 / 12",
    );
    expect(
      wrapper.get('[data-testid="lfs-reconcile-start"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.get('[data-testid="lfs-reconcile-stats"]').text()).toContain(
      "history added",
    );

    await vi.advanceTimersByTimeAsync(3000);
    await flushPromises();

    expect(api.getLfsReconciliation).toHaveBeenCalledTimes(2);
    expect(wrapper.get('[data-testid="lfs-reconcile-task"]').text()).toContain(
      "finished",
    );
    expect(wrapper.get('[data-testid="lfs-reconciled-at"]').text()).toBe(
      "2026-09-27 10:05:00",
    );
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.getLfsReconciliation).toHaveBeenCalledTimes(2); // no more polling
    wrapper.unmount();
  });

  it("starts a reconciliation, or reports that one is already scheduled", async () => {
    const idle = {
      reconciled_at: "2026-09-27T10:05:00",
      auto_gc: true,
      task: task({ status: "succeeded", progress_total: 0, stats: {} }),
    };
    api.getLfsReconciliation.mockResolvedValue(idle);
    api.startLfsReconciliation
      .mockResolvedValueOnce({ task_id: 9, already_pending: false })
      .mockResolvedValueOnce({ task_id: null, already_pending: true });
    const wrapper = mountPanel();
    await flushPromises();
    expect(wrapper.find('[data-testid="lfs-reconcile-stats"]').exists()).toBe(
      false,
    );

    await wrapper.get('[data-testid="lfs-reconcile-start"]').trigger("click");
    await flushPromises();
    expect(api.startLfsReconciliation).toHaveBeenCalledWith("admin-token");
    expect(dialogs.success).toHaveBeenLastCalledWith(
      "Reconciliation scheduled (task #9)",
    );

    await wrapper.get('[data-testid="lfs-reconcile-refresh"]').trigger("click");
    await wrapper.get('[data-testid="lfs-reconcile-start"]').trigger("click");
    await flushPromises();
    expect(dialogs.success).toHaveBeenLastCalledWith(
      "A reconciliation is already scheduled",
    );
  });

  it("shows a queued task before its first progress report", async () => {
    api.getLfsReconciliation.mockResolvedValue({
      reconciled_at: null,
      auto_gc: true,
      task: task({
        status: "queued",
        progress_done: null,
        progress_total: null,
        stage: null,
        stats: {},
      }),
    });
    const wrapper = mountPanel();
    await flushPromises();

    expect(wrapper.get('[data-testid="lfs-reconcile-task"]').text()).toContain(
      "0 / ?",
    );
    expect(wrapper.get('[data-testid="lfs-reconcile-start"]').text()).toBe(
      "Running",
    );
    wrapper.unmount();
  });

  it("reports failures to the page", async () => {
    const failure = new Error("boom");
    api.getLfsReconciliation.mockRejectedValue(failure);
    api.startLfsReconciliation.mockRejectedValue(failure);
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.get('[data-testid="lfs-reconcile-start"]').trigger("click");
    await flushPromises();

    expect(wrapper.emitted("error")).toEqual([[failure], [failure]]); // load, start
  });
});
