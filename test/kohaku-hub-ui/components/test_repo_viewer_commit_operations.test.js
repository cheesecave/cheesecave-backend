import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { http, HttpResponse } from "@/testing/msw";
import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";
import {
  cloneFixture,
  jsonResponse,
  uiApiFixtures,
} from "../helpers/api-fixtures";
import { server } from "../setup/msw-server";

vi.mock("vue-router/auto", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));

import RepoViewer from "@/components/repo/RepoViewer.vue";

const BASE = "/api/datasets/open-media-lab/badges";

// The real tooltip only renders its content on hover
const TooltipStub = {
  props: { content: { type: String, default: "" } },
  template: '<span class="tooltip" :data-content="content"><slot /></span>',
};

function commit(id, title) {
  return {
    id,
    title,
    message: title,
    date: "2026-04-21T10:00:00.000000Z",
    authors: [{ user: "alice" }],
  };
}

const unavailable = (reason, message) => ({
  available: false,
  reason,
  message,
});

describe("RepoViewer commit operation badges", () => {
  const posted = [];

  function install({ caps = { revert: true, reset: true }, verdicts, pages }) {
    posted.length = 0;
    server.use(
      http.get("/api/users/open-media-lab/type", () =>
        jsonResponse({ type: "org" }),
      ),
      http.get(BASE, () =>
        jsonResponse({
          ...cloneFixture(uiApiFixtures.repo.info),
          id: "open-media-lab/badges",
        }),
      ),
      http.get("/api/site-config", () =>
        jsonResponse({
          capabilities: { repository_operations: { ...caps, squash: false } },
        }),
      ),
      http.get(`${BASE}/commits/main`, ({ request }) => {
        const after = new URL(request.url).searchParams.get("after");
        const page = pages[after || "first"];
        const headers = page.next
          ? { Link: `<${BASE}/commits/main?after=${page.next}>; rel="next"` }
          : {};
        return HttpResponse.json(page.commits, { headers });
      }),
      http.post(`${BASE}/commits/main/operations`, async ({ request }) => {
        const { commit_ids: ids } = await request.json();
        posted.push(ids);
        if (verdicts === "fail") return HttpResponse.json({}, { status: 500 });
        return jsonResponse({
          can_write: true,
          commits: Object.fromEntries(
            ids.map((id) => [id, verdicts[id]]).filter(([, v]) => v),
          ),
        });
      }),
    );
  }

  beforeEach(() => {
    vi.clearAllMocks();
    setActivePinia(createPinia());
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  function mountViewer() {
    return mount(RepoViewer, {
      props: {
        repoType: "dataset",
        namespace: "open-media-lab",
        name: "badges",
        branch: "main",
        currentPath: "",
        tab: "commits",
      },
      global: {
        stubs: {
          ...ElementPlusStubs,
          ElTooltip: TooltipStub,
          RouterLink: RouterLinkStub,
          MarkdownViewer: true,
          MetadataHeader: true,
          DetailedMetadataPanel: true,
          ReferencedDatasetsCard: true,
          SidebarRelationshipsCard: true,
          DatasetViewerTab: true,
        },
      },
    });
  }

  function badges(wrapper) {
    return wrapper.findAll('[data-testid^="commit-"]').map((tag) => ({
      id: tag.attributes("data-testid"),
      text: tag.text(),
      tooltip: tag.element.parentElement.getAttribute("data-content"),
    }));
  }

  it("marks only what is proven unavailable, page by page", async () => {
    install({
      pages: {
        first: {
          commits: [commit("c3", "Third"), commit("c2", "Second")],
          next: "p2",
        },
        p2: { commits: [commit("c1", "First")] },
      },
      verdicts: {
        c3: {
          revert: { available: null },
          reset: unavailable(
            "already_current",
            "The branch is already at this commit.",
          ),
        },
        c2: { revert: { available: null }, reset: { available: null } },
        c1: {
          revert: unavailable(
            "initial_commit",
            "This is the repository's first commit.",
          ),
          reset: unavailable("disabled", "Reset is disabled on this site."),
        },
      },
    });
    const wrapper = mountViewer();
    await flushPromises();
    await flushPromises();

    expect(posted).toEqual([["c3", "c2"]]);
    expect(badges(wrapper)).toEqual([
      {
        id: "commit-reset-unavailable-c3",
        text: "Can't reset",
        tooltip: "The branch is already at this commit.",
      },
    ]);

    const more = wrapper
      .findAll("button")
      .find((b) => b.text().includes("Load More Commits"));
    await more.trigger("click");
    await flushPromises();
    await flushPromises();
    expect(posted).toEqual([["c3", "c2"], ["c1"]]);
    // A site-wide switch is not about the commit: no badge for it
    expect(badges(wrapper).map((b) => b.id)).toEqual([
      "commit-reset-unavailable-c3",
      "commit-revert-unavailable-c1",
    ]);
  });

  it("asks nothing while both operations are off, and shrugs off failures", async () => {
    install({
      caps: { revert: false, reset: false },
      pages: { first: { commits: [commit("c1", "First")] } },
      verdicts: {},
    });
    let wrapper = mountViewer();
    await flushPromises();
    await flushPromises();
    expect(posted).toEqual([]);
    expect(badges(wrapper)).toEqual([]);

    install({
      pages: { first: { commits: [commit("c1", "First")] } },
      verdicts: "fail",
    });
    wrapper = mountViewer();
    await flushPromises();
    await flushPromises();
    expect(posted).toEqual([["c1"]]);
    expect(badges(wrapper)).toEqual([]);
    expect(wrapper.text()).toContain("First"); // the list itself is unaffected
  });
});
