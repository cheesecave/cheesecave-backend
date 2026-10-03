import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { getFrontendBuildInfo } from "../../../scripts/frontend-build-info.mjs";

const temporaryRoots = [];
const unknownBuild = { commit: "unknown", dirty: false };
const overrideCommit = "abcdef0123".repeat(4);

function createRoot() {
  const root = mkdtempSync(join(tmpdir(), "kohakuhub-build-info-"));
  temporaryRoots.push(root);
  return root;
}

function git(repoRoot, ...args) {
  return execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
}

function createRepository() {
  const root = createRoot();
  git(root, "init");
  writeFileSync(join(root, "tracked.txt"), "original\n");
  git(root, "add", "tracked.txt");
  git(
    root,
    "-c",
    "user.name=Build Info Test",
    "-c",
    "user.email=build-info@example.test",
    "-c",
    "commit.gpgsign=false",
    "commit",
    "-m",
    "initial",
  );
  return root;
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("frontend Git build information", () => {
  it("resolves the full commit from the supplied repository instead of process cwd", () => {
    const repoRoot = createRepository();
    expect(getFrontendBuildInfo({ repoRoot, env: {} })).toEqual({
      commit: git(repoRoot, "rev-parse", "HEAD"),
      dirty: false,
    });
  });

  it("detects both unstaged and staged tracked changes", () => {
    const repoRoot = createRepository();
    writeFileSync(join(repoRoot, "tracked.txt"), "changed\n");
    expect(getFrontendBuildInfo({ repoRoot, env: {} }).dirty).toBe(true);
    git(repoRoot, "add", "tracked.txt");
    expect(getFrontendBuildInfo({ repoRoot, env: {} }).dirty).toBe(true);
  });

  it("ignores untracked files", () => {
    const repoRoot = createRepository();
    writeFileSync(join(repoRoot, "untracked.txt"), "local file\n");
    expect(getFrontendBuildInfo({ repoRoot, env: {} }).dirty).toBe(false);
  });

  it("does not borrow a parent repository's commit for a source archive", () => {
    const parentRoot = createRepository();
    const repoRoot = join(parentRoot, "archive");
    mkdirSync(repoRoot);
    expect(getFrontendBuildInfo({ repoRoot, env: {} })).toEqual(unknownBuild);
  });

  it("supports linked worktrees with a .git file", () => {
    const parentRoot = createRepository();
    const repoRoot = join(parentRoot, "worktree");
    git(parentRoot, "worktree", "add", "--detach", repoRoot, "HEAD");
    expect(getFrontendBuildInfo({ repoRoot, env: {} })).toEqual({
      commit: git(parentRoot, "rev-parse", "HEAD"),
      dirty: false,
    });
  });

  it("falls back when Git metadata cannot be read", () => {
    const repoRoot = createRoot();
    writeFileSync(join(repoRoot, ".git"), "gitdir: missing\n");
    expect(getFrontendBuildInfo({ repoRoot, env: {} })).toEqual(unknownBuild);
  });

  it("uses an explicit commit for archives and its explicit dirty value", () => {
    const repoRoot = createRoot();
    expect(
      getFrontendBuildInfo({
        repoRoot,
        env: { VITE_GIT_COMMIT: overrideCommit, VITE_GIT_DIRTY: "true" },
      }),
    ).toEqual({ commit: overrideCommit, dirty: true });
  });

  it("does not attach the local checkout's dirty state to an overridden commit", () => {
    const repoRoot = createRepository();
    writeFileSync(join(repoRoot, "tracked.txt"), "changed\n");
    expect(
      getFrontendBuildInfo({
        repoRoot,
        env: { VITE_GIT_COMMIT: overrideCommit },
      }),
    ).toEqual({ commit: overrideCommit, dirty: false });
  });

  it("allows explicit dirty overrides for the local commit", () => {
    const repoRoot = createRepository();
    expect(
      getFrontendBuildInfo({ repoRoot, env: { VITE_GIT_DIRTY: "true" } }).dirty,
    ).toBe(true);
    writeFileSync(join(repoRoot, "tracked.txt"), "changed\n");
    expect(
      getFrontendBuildInfo({ repoRoot, env: { VITE_GIT_DIRTY: "false" } })
        .dirty,
    ).toBe(false);
  });

  it("rejects invalid commit overrides rather than showing unrelated local metadata", () => {
    expect(
      getFrontendBuildInfo({
        repoRoot: createRepository(),
        env: { VITE_GIT_COMMIT: "not-a-commit" },
      }),
    ).toEqual(unknownBuild);
  });

  it("accepts SHA-256 commits and ignores invalid dirty override values", () => {
    const commit = "a".repeat(64);
    expect(
      getFrontendBuildInfo({
        repoRoot: createRoot(),
        env: { VITE_GIT_COMMIT: commit, VITE_GIT_DIRTY: "invalid" },
      }),
    ).toEqual({ commit, dirty: false });
  });
});
