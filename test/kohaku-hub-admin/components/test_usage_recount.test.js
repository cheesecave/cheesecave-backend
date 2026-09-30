import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const api = vi.hoisted(() => ({
  getUsageRecount: vi.fn(),
  startUsageRecount: vi.fn(),
}));
const dialogs = vi.hoisted(() => ({ success: vi.fn() }));

vi.mock("@/utils/api", () => ({
  getUsageRecount: (...args) => api.getUsageRecount(...args),
  startUsageRecount: (...args) => api.startUsageRecount(...args),
  formatBytes: (bytes) => `${bytes} B`,
}));

import UsageRecount from "@/components/storage/UsageRecount.vue";

function task(overrides = {}) {
  return {
    id: 7,
    status: "running",
    progress_done: 3,
    progress_total: 12,
    stage: "recounting model:owner/demo",
    stats: {},
    drift: [],
    created_at: "2026-09-30T10:00:00+00:00",
    finished_at: null,
    ...overrides,
  };
}

function mountPanel() {
  return mount(UsageRecount, {
    props: { token: "admin-token" },
    global: {
      stubs: {
        ...ElementPlusStubs,
        RouterLink: { template: "<a><slot /></a>" },
      },
    },
  });
}

describe("UsageRecount", () => {
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

  it("says when no recount has run and whether it repeats", async () => {
    api.getUsageRecount.mockResolvedValue({ interval_hours: 0, task: null });
    const wrapper = mountPanel();
    await flushPromises();

    expect(api.getUsageRecount).toHaveBeenCalledWith("admin-token");
    expect(wrapper.get('[data-testid="usage-recount-interval"]').text()).toBe(
      "off",
    );
    expect(wrapper.find('[data-testid="usage-recount-none"]').exists()).toBe(
      true,
    );
  });

  it("follows a running recount until it reports the drift", async () => {
    api.getUsageRecount
      .mockResolvedValueOnce({ interval_hours: 24, task: task() })
      .mockResolvedValueOnce({
        interval_hours: 24,
        task: task({
          status: "succeeded",
          progress_done: 12,
          finished_at: "2026-09-30T10:05:00+00:00",
          stats: { repositories: 12, drifted: 2, drift_bytes: 300, busy: 1 },
          drift: [
            { repository: "model:owner/a", before: 500, after: 300 },
            { repository: "model:owner/b", before: 0, after: 100 },
            { repository: "model:owner/c", before: 5, after: 5 },
          ],
        }),
      });
    const wrapper = mountPanel();
    await flushPromises();

    expect(wrapper.get('[data-testid="usage-recount-interval"]').text()).toBe(
      "every 24 h",
    );
    expect(wrapper.get('[data-testid="usage-recount-task"]').text()).toContain(
      "3 / 12",
    );
    expect(
      wrapper.get('[data-testid="usage-recount-start"]').attributes("disabled"),
    ).toBeDefined();
    expect(wrapper.find('[data-testid="usage-recount-stats"]').exists()).toBe(
      false,
    );

    await vi.advanceTimersByTimeAsync(3000);
    await flushPromises();

    expect(api.getUsageRecount).toHaveBeenCalledTimes(2);
    const stats = wrapper.get('[data-testid="usage-recount-stats"]').text();
    expect(stats).toContain("Recounted: 12");
    expect(stats).toContain("Drifted: 2");
    expect(stats).toContain("300 B in all");
    expect(stats).toContain("recounted later): 1");
    const drift = wrapper.get('[data-testid="usage-recount-drift"]').text();
    expect(drift).toContain("model:owner/a");
    expect(drift).toContain("-200 B");
    expect(drift).toContain("+100 B");
    expect(drift).toContain("0 B");
    expect(wrapper.get('[data-testid="usage-recount-task"]').text()).toContain(
      "finished",
    );
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.getUsageRecount).toHaveBeenCalledTimes(2); // no more polling
    wrapper.unmount();
  });

  it("starts a recount, or reports that one is already scheduled", async () => {
    api.getUsageRecount.mockResolvedValue({
      interval_hours: 0,
      task: task({
        status: "succeeded",
        progress_total: 0,
        stats: { repositories: 4 },
      }),
    });
    api.startUsageRecount
      .mockResolvedValueOnce({ task_id: 9, already_pending: false })
      .mockResolvedValueOnce({ task_id: null, already_pending: true });
    const wrapper = mountPanel();
    await flushPromises();
    const stats = wrapper.get('[data-testid="usage-recount-stats"]').text();
    expect(stats).toContain("Drifted: 0");
    expect(stats).not.toContain("recounted later");
    expect(wrapper.find('[data-testid="usage-recount-drift"]').exists()).toBe(
      false,
    );

    await wrapper.get('[data-testid="usage-recount-start"]').trigger("click");
    await flushPromises();
    expect(api.startUsageRecount).toHaveBeenCalledWith("admin-token");
    expect(dialogs.success).toHaveBeenLastCalledWith(
      "Recount scheduled (task #9)",
    );

    await wrapper.get('[data-testid="usage-recount-refresh"]').trigger("click");
    await wrapper.get('[data-testid="usage-recount-start"]').trigger("click");
    await flushPromises();
    expect(dialogs.success).toHaveBeenLastCalledWith(
      "A recount is already scheduled",
    );
  });

  it("shows a queued recount before its first progress report", async () => {
    api.getUsageRecount.mockResolvedValue({
      interval_hours: 0,
      task: task({
        status: "queued",
        progress_done: null,
        progress_total: null,
        stage: null,
      }),
    });
    const wrapper = mountPanel();
    await flushPromises();

    expect(wrapper.get('[data-testid="usage-recount-task"]').text()).toContain(
      "0 / ?",
    );
    expect(wrapper.get('[data-testid="usage-recount-start"]').text()).toBe(
      "Running",
    );
    wrapper.unmount();
  });

  it("reports failures to the page", async () => {
    const failure = new Error("boom");
    api.getUsageRecount.mockRejectedValue(failure);
    api.startUsageRecount.mockRejectedValue(failure);
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.get('[data-testid="usage-recount-start"]').trigger("click");
    await flushPromises();

    expect(wrapper.emitted("error")).toEqual([[failure], [failure]]); // load, start
  });
});
