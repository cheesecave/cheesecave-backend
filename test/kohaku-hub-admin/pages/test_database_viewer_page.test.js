import { defineComponent, h } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn() },
  adminStore: { token: "admin-token" },
  api: {
    listDatabaseTables: vi.fn(),
    getDatabaseQueryTemplates: vi.fn(),
    executeDatabaseQuery: vi.fn(),
  },
}));

vi.mock("vue-router", () => ({
  useRouter: () => mocks.router,
}));

vi.mock("@/stores/admin", () => ({
  useAdminStore: () => mocks.adminStore,
}));

vi.mock("@/utils/api", () => ({
  listDatabaseTables: (...args) => mocks.api.listDatabaseTables(...args),
  getDatabaseQueryTemplates: (...args) =>
    mocks.api.getDatabaseQueryTemplates(...args),
  executeDatabaseQuery: (...args) => mocks.api.executeDatabaseQuery(...args),
}));

vi.mock("@/components/AdminLayout.vue", () => ({
  default: defineComponent({
    name: "AdminLayoutStub",
    setup(_, { slots }) {
      return () => h("main", slots.default ? slots.default() : []);
    },
  }),
}));

import DatabaseViewerPage from "@/pages/DatabaseViewer.vue";

let elementPlusModule;
let wrapper;

function mountPage() {
  wrapper = mount(DatabaseViewerPage, {
    global: {
      components: { ElAlert: elementPlusModule.ElAlert },
      stubs: { ...ElementPlusStubs, ElAlert: false },
      directives: { loading: { mounted() {}, updated() {} } },
    },
  });
  return wrapper;
}

function getExecuteButton(page) {
  return page
    .findAll("button")
    .find((button) => button.text().includes("Execute Query"));
}

describe("admin database viewer page", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    mocks.adminStore.token = "admin-token";
    mocks.api.listDatabaseTables.mockResolvedValue({ tables: [] });
    mocks.api.getDatabaseQueryTemplates.mockResolvedValue({ templates: [] });
    elementPlusModule ??= await vi.importActual("element-plus");
  });

  afterEach(() => {
    wrapper?.unmount();
  });

  it("shows the read-only notice before the page header and keeps query controls in the editor", async () => {
    const page = mountPage();
    await flushPromises();

    const notice = page
      .get('[data-testid="database-read-only-notice"]')
      .get('[role="alert"]');
    const header = page.get(".admin-page-header");
    const editor = page.get(".sql-editor");
    const executeButton = getExecuteButton(page);

    expect(notice.text()).toContain(
      "Read-only mode - Only SELECT queries allowed",
    );
    expect(notice.isVisible()).toBe(true);
    expect(
      notice.element.compareDocumentPosition(header.element) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(header.get("h1").text()).toBe("Database Viewer");
    expect(header.text()).toContain(
      "Browse database tables and run read-only SQL queries.",
    );
    expect(header.element.contains(notice.element)).toBe(false);
    expect(header.element.contains(editor.element)).toBe(false);
    expect(header.element.contains(executeButton.element)).toBe(false);
    expect(editor.get("input").attributes("placeholder")).toContain(
      "Enter your SELECT query here...",
    );
    expect(page.get(".editor-section").element.contains(editor.element)).toBe(
      true,
    );
    expect(
      page.get(".editor-section").element.contains(executeButton.element),
    ).toBe(true);
  });

  it("closes the notice without removing the header, query text, or editor actions", async () => {
    const page = mountPage();
    await flushPromises();
    const notice = page
      .get('[data-testid="database-read-only-notice"]')
      .get('[role="alert"]');
    const query = "SELECT * FROM repository LIMIT 10;";
    await page.get(".sql-editor input").setValue(query);

    await notice.get(".el-alert__close-btn").trigger("click");
    await flushPromises();
    expect(notice.isVisible()).toBe(false);
    expect(page.get(".admin-page-header").isVisible()).toBe(true);
    expect(page.get(".sql-editor").isVisible()).toBe(true);
    expect(page.get(".sql-editor input").element.value).toBe(query);
    expect(getExecuteButton(page).isVisible()).toBe(true);
    const clearButton = page
      .findAll("button")
      .find((button) => button.text() === "Clear");
    await clearButton.trigger("click");
    expect(page.get(".sql-editor input").element.value).toBe("");
  });
});
