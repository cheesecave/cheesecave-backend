import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { http, HttpResponse } from "@/testing/msw";
import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";
import { cloneFixture, jsonResponse, uiApiFixtures } from "../helpers/api-fixtures";
import { server } from "../setup/msw-server";

vi.mock("vue-router/auto", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));

import RepoViewer from "@/components/repo/RepoViewer.vue";

const BASE = "/api/datasets/open-media-lab/big-repo";

function deferred() {
  let resolve;
  const promise = new Promise((res) => (resolve = res));
  return { promise, resolve };
}

function mountViewer(tab) {
  return mount(RepoViewer, {
    props: {
      repoType: "dataset",
      namespace: "open-media-lab",
      name: "big-repo",
      branch: "main",
      currentPath: "",
      tab,
    },
    global: {
      stubs: {
        ...ElementPlusStubs,
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

// The repository page's file list comes from the paginated tree; the
// whole-repository sibling list in repo info grows with the repository
// (#101), so the page asks only for its own fields.
describe("RepoViewer loading on a big repository", () => {
  const seen = [];
  let infoGate;

  beforeEach(() => {
    seen.length = 0;
    infoGate = null;
    setActivePinia(createPinia());
    vi.spyOn(console, "error").mockImplementation(() => {});
    server.use(
      http.get("/api/users/open-media-lab/type", () => jsonResponse({ type: "org" })),
      http.get(BASE, async ({ request }) => {
        const url = new URL(request.url);
        seen.push({ what: "info", expand: url.searchParams.getAll("expand") });
        if (infoGate) await infoGate.promise;
        const { siblings, ...info } = cloneFixture(uiApiFixtures.repo.info);
        return jsonResponse({ ...info, id: "open-media-lab/big-repo" });
      }),
      http.get(`${BASE}/tree/main`, () => {
        seen.push({ what: "tree" });
        return jsonResponse([
          { type: "file", path: "README.md", size: 9, oid: "r" },
          ...cloneFixture(uiApiFixtures.repo.tree),
        ]);
      }),
      http.post(`${BASE}/paths-info/main`, () => jsonResponse([])),
      http.get("/datasets/open-media-lab/big-repo/resolve/main/README.md", () => {
        seen.push({ what: "readme" });
        return new HttpResponse("# Big repo\n", { status: 200 });
      }),
    );
  });

  it("asks repo info for the page's fields only, never the file list", async () => {
    mountViewer("files");
    await flushPromises();

    const info = seen.find((s) => s.what === "info");
    expect(info.expand).toEqual(
      expect.arrayContaining(["sha", "lastModified", "private", "likes", "tags", "storage"]),
    );
    expect(info.expand).not.toContain("siblings");
  });

  it("starts the file list without waiting for repo info", async () => {
    infoGate = deferred();
    const wrapper = mountViewer("files");
    await flushPromises();

    expect(seen.map((s) => s.what)).toEqual(["info", "tree"]);

    infoGate.resolve();
    await flushPromises();
    await flushPromises();
    expect(wrapper.text()).toContain("README.md");
  });

  it("fetches the README once on the card tab", async () => {
    mountViewer("card");
    await flushPromises();
    await flushPromises();
    await flushPromises();

    expect(seen.filter((s) => s.what === "readme")).toHaveLength(1);
  });
});
