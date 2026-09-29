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
  router: { push: vi.fn(), back: vi.fn() },
  settingsAPI: {
    getSiteConfig: vi.fn(),
    revertBranch: vi.fn(),
    resetBranch: vi.fn(),
  },
  repoAPI: { getCommitOperations: vi.fn(), getCommitUnavailableFiles: vi.fn() },
}));

vi.mock("vue-router/auto", () => ({
  useRoute: () => mocks.route,
  useRouter: () => mocks.router,
}));

vi.mock("@/utils/api", () => ({
  settingsAPI: mocks.settingsAPI,
  repoAPI: mocks.repoAPI,
}));

import CommitPage from "@/pages/[type]s/[namespace]/[name]/commit/[commit_id].vue";

// The real tooltip only renders its content on hover
const TooltipStub = {
  props: { content: { type: String, default: "" }, disabled: Boolean },
  template:
    '<div class="tooltip" :data-content="content" :data-disabled="String(disabled)"><slot /></div>',
};

// Renders the title slot, where the file tags are
const CollapseItemStub = {
  template: '<div class="collapse-item"><slot name="title" /><slot /></div>',
};

function mountPage() {
  return mount(CommitPage, {
    global: {
      stubs: {
        ...ElementPlusStubs,
        ElTooltip: TooltipStub,
        ElCollapseItem: CollapseItemStub,
        RouterLink: RouterLinkStub,
      },
    },
  });
}

function action(wrapper, op) {
  const holder = wrapper.get(`[data-testid="${op}-action"]`);
  return {
    disabled: holder.get("button").attributes("disabled") !== undefined,
    tooltip: holder.element.parentElement.getAttribute("data-content"),
    tooltipOff: holder.element.parentElement.getAttribute("data-disabled"),
  };
}

function checks(revert, reset) {
  return {
    data: {
      can_write: true,
      operations: { revert: true, reset: true },
      revert,
      reset,
    },
  };
}

describe("commit page operation checks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(axios, "get").mockImplementation((url) =>
      Promise.resolve({
        data: url.endsWith("/diff")
          ? { files: [] }
          : {
              commit_id: "commit-1",
              message: "A commit",
              author: "owner",
              date: 1,
            },
      }),
    );
    mocks.repoAPI.getCommitUnavailableFiles.mockResolvedValue({
      data: { files: [] },
    });
    mocks.settingsAPI.getSiteConfig.mockResolvedValue({
      data: {
        capabilities: {
          repository_operations: { revert: true, reset: true, squash: false },
        },
      },
    });
  });

  it("greys out what the commit cannot do, with the reason", async () => {
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks(
        {
          available: false,
          reason: "conflict",
          message: "Later commits changed the same files: a.bin.",
        },
        { available: true, files: 3, requires_force: true },
      ),
    );
    const wrapper = mountPage();
    await flushPromises();

    expect(mocks.repoAPI.getCommitOperations).toHaveBeenCalledWith(
      "model",
      "owner",
      "demo",
      "commit-1",
      "main",
    );
    expect(action(wrapper, "revert")).toEqual({
      disabled: true,
      tooltip: "Later commits changed the same files: a.bin.",
      tooltipOff: "false",
    });
    expect(action(wrapper, "reset")).toEqual({
      disabled: false,
      tooltip: "",
      tooltipOff: "true",
    });

    // The reset dialog tells what it will do
    await wrapper.get('[data-testid="reset-action"] button').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-testid="reset-scope"]').text()).toContain(
      "Restores 3 file(s)",
    );
    expect(wrapper.find('[data-testid="revert-scope"]').exists()).toBe(false);
  });

  it("explains missing write access the same way", async () => {
    const forbidden = {
      available: false,
      reason: "forbidden",
      message: "You need write access to this repository.",
    };
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks(forbidden, forbidden),
    );
    const wrapper = mountPage();
    await flushPromises();

    for (const op of ["revert", "reset"]) {
      expect(action(wrapper, op)).toMatchObject({
        disabled: true,
        tooltip: "You need write access to this repository.",
      });
    }
  });

  it("waits for the check, and leaves the actions usable when it fails", async () => {
    let settle;
    mocks.repoAPI.getCommitOperations.mockReturnValue(
      new Promise((_resolve, reject) => {
        settle = reject;
      }),
    );
    const wrapper = mountPage();
    await flushPromises();
    expect(action(wrapper, "revert")).toMatchObject({
      disabled: true,
      tooltip: "Checking whether this is possible…",
    });

    settle(new Error("offline"));
    await flushPromises();
    // The server still checks when the action runs
    expect(action(wrapper, "revert")).toMatchObject({
      disabled: false,
      tooltip: "",
    });
    expect(action(wrapper, "reset")).toMatchObject({
      disabled: false,
      tooltip: "",
    });
  });

  it("explains, without blocking, what is too large to check in advance", async () => {
    const tooLarge = {
      available: null,
      reason: "too_large",
      message:
        "Revert touches more files than can be checked in advance; it is checked when it runs.",
    };
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks(tooLarge, { available: true, files: 1 }),
    );
    const wrapper = mountPage();
    await flushPromises();

    expect(action(wrapper, "revert")).toEqual({
      disabled: false,
      tooltip: tooLarge.message,
      tooltipOff: "false",
    });
    expect(action(wrapper, "reset")).toMatchObject({
      disabled: false,
      tooltip: "",
    });
  });

  it("shows the revert scope and asks nothing while both actions are off", async () => {
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks({ available: true, files: 2 }, { available: true, files: 1 }),
    );
    let wrapper = mountPage();
    await flushPromises();
    await wrapper.get('[data-testid="revert-action"] button').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-testid="revert-scope"]').text()).toContain(
      "Undoes the changes to 2 file(s)",
    );

    mocks.repoAPI.getCommitOperations.mockClear();
    mocks.settingsAPI.getSiteConfig.mockResolvedValue({ data: {} });
    wrapper = mountPage();
    await flushPromises();
    expect(mocks.repoAPI.getCommitOperations).not.toHaveBeenCalled();
    expect(wrapper.find('[data-testid="revert-action"]').exists()).toBe(false);
  });

  it("lists every file of the commit that is no longer stored", async () => {
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks({ available: true, files: 1 }, { available: true, files: 1 }),
    );
    mocks.repoAPI.getCommitUnavailableFiles.mockResolvedValue({
      data: {
        files: [
          { path: "weights.bin", sha256: "a".repeat(64) },
          { path: "extra/old.bin", sha256: "b".repeat(64) },
        ],
      },
    });
    axios.get.mockImplementation((url) =>
      Promise.resolve({
        data: url.endsWith("/diff")
          ? {
              files: [
                {
                  path: "weights.bin",
                  type: "changed",
                  is_lfs: true,
                  lfs_status: "collected",
                  previous_lfs_status: "missing",
                },
                {
                  path: "tokenizer.bin",
                  type: "added",
                  is_lfs: true,
                  lfs_status: "available",
                },
              ],
            }
          : {
              commit_id: "commit-1",
              message: "A commit",
              author: "owner",
              date: 1,
            },
      }),
    );
    const wrapper = mountPage();
    await flushPromises();

    expect(mocks.repoAPI.getCommitUnavailableFiles).toHaveBeenCalledWith(
      "model",
      "owner",
      "demo",
      "commit-1",
      "main",
    );
    const panel = wrapper.get('[data-testid="unavailable-files"]');
    expect(panel.text()).toContain(
      "2 file(s) of this commit are no longer stored",
    );
    expect(panel.text()).toContain("extra/old.bin");
    const marks = wrapper
      .findAll('[data-testid="file-unavailable-weights.bin"]')
      .map((tag) => [
        tag.text(),
        tag.element.parentElement.getAttribute("data-content"),
      ]);
    expect(marks).toEqual([
      ["Unavailable", "This version is no longer stored: garbage collected."],
      [
        "Previous version unavailable",
        "The version before this commit is no longer stored: missing from storage.",
      ],
    ]);
    expect(
      wrapper.find('[data-testid="file-unavailable-tokenizer.bin"]').exists(),
    ).toBe(false);
  });

  it("shows no list when nothing is lost or it cannot be told", async () => {
    mocks.repoAPI.getCommitOperations.mockResolvedValue(
      checks({ available: true, files: 1 }, { available: true, files: 1 }),
    );
    mocks.repoAPI.getCommitUnavailableFiles.mockResolvedValue({
      data: { files: null, reason: "too_large" },
    });
    let wrapper = mountPage();
    await flushPromises();
    expect(wrapper.find('[data-testid="unavailable-files"]').exists()).toBe(
      false,
    );

    mocks.repoAPI.getCommitUnavailableFiles.mockRejectedValue(
      new Error("offline"),
    );
    wrapper = mountPage();
    await flushPromises();
    expect(wrapper.find('[data-testid="unavailable-files"]').exists()).toBe(
      false,
    );
  });
});
