// Component test for the standalone blob page's media preview: which
// files render as an image, audio or video, and what shows when the
// browser cannot decode an image (kohakuhub media types).

import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";

const mocks = vi.hoisted(() => ({
  route: {
    path: "/datasets/open-media-lab/showcase/blob/main/archives/models/bundle.tar",
    params: {
      namespace: "open-media-lab",
      name: "showcase",
      branch: "main",
      file: "archives/models/bundle.tar",
    },
    query: {},
  },
  listTreeImpl: vi.fn(),
}));

vi.mock("vue-router/auto", () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));

vi.mock("@/utils/api", () => ({
  repoAPI: {
    listTree: (...args) => mocks.listTreeImpl(...args),
    commitFiles: vi.fn(),
  },
}));

vi.mock("@/stores/auth", () => ({
  useAuthStore: () => ({
    isAuthenticated: false,
    canWriteToNamespace: () => false,
  }),
}));

vi.mock("@/utils/clipboard", () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}));

vi.mock("@/utils/http-errors", () => ({
  classifyError: (err) => ({ kind: "generic", detail: String(err) }),
  classifyResponse: (resp) => ({ kind: "not-found", status: resp?.status }),
  downloadToastFor: (c) => `download-toast:${c?.kind || "?"}`,
  probeUrlAndClassify: vi.fn().mockResolvedValue({ ok: true }),
  ERROR_KIND: { NOT_FOUND: "not-found", UPSTREAM_UNAVAILABLE: "upstream-unavailable" },
}));

// The page's TarBrowserPanel mount is the surface we want to assert.
// Stub it as a presence-detector that surfaces forwarded props as
// data attributes.
vi.mock("@/components/repo/preview/TarBrowserPanel.vue", () => ({
  default: {
    name: "TarBrowserPanel",
    props: ["tarUrl", "indexUrl", "filename", "tarTreeEntry"],
    template:
      '<div data-stub="TarBrowserPanel" :data-tar-url="tarUrl" :data-index-url="indexUrl" :data-filename="filename" :data-has-tree-entry="tarTreeEntry ? \'true\' : \'false\'" />',
  },
}));

// Make the body-fetch path inert so the test doesn't loop on
// retries while the indexed-tar detection runs in parallel.
const fetchMock = vi.fn(async () =>
  new Response(new Uint8Array([0x00, 0x01]), {
    status: 200,
    headers: new Headers({ "Content-Length": "2", "X-Error-Code": "" }),
  }),
);

import BlobPage from "@/pages/[type]s/[namespace]/[name]/blob/[branch]/[...file].vue";

function mountBlob() {
  return mount(BlobPage, {
    global: {
      stubs: {
        ...ElementPlusStubs,
        RouterLink: RouterLinkStub,
        MarkdownViewer: { template: "<div data-stub=MarkdownViewer />" },
        CodeViewer: { template: "<div data-stub=CodeViewer />" },
        ErrorState: { template: "<div data-stub=ErrorState />" },
      },
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  globalThis.fetch = fetchMock;
});

function openFile(file) {
  mocks.route.path = `/datasets/open-media-lab/showcase/blob/main/${file}`;
  mocks.route.params = {
    namespace: "open-media-lab",
    name: "showcase",
    branch: "main",
    file,
  };
}

describe("blob page · media preview", () => {
  it.each(["gallery/cover.avif", "gallery/anim.apng", "gallery/scraped.jfif"])(
    "previews %s as an image",
    async (file) => {
      openFile(file);
      const wrapper = mountBlob();
      await flushPromises();

      const img = wrapper.find(`img[alt="${file.split("/").pop()}"]`);
      expect(img.exists()).toBe(true);
      expect(img.attributes("src")).toContain(file);
    },
  );

  it("says so, instead of a broken image, when the browser cannot decode it", async () => {
    openFile("gallery/cover.avif");
    const wrapper = mountBlob();
    await flushPromises();

    await wrapper.find('img[alt="cover.avif"]').trigger("error");

    expect(wrapper.find('img[alt="cover.avif"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("This browser cannot display this image");
  });

  it("plays an .ogg voice line as audio, not as a video", async () => {
    openFile("voices/line_001.ogg");
    const wrapper = mountBlob();
    await flushPromises();

    expect(wrapper.find("audio").exists()).toBe(true);
    expect(wrapper.find("video").exists()).toBe(false);
  });

  it("plays an .mkv as a video", async () => {
    openFile("clips/op.mkv");
    const wrapper = mountBlob();
    await flushPromises();

    expect(wrapper.find("video").exists()).toBe(true);
  });
});
