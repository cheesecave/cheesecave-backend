import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const defaultRepoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const unknownBuild = { commit: "unknown", dirty: false };
const commitPattern = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/i;

/** Resolve a build-time snapshot for both frontends, including source archives. */
export function getFrontendBuildInfo({
  repoRoot = defaultRepoRoot,
  env = process.env,
} = {}) {
  const commitOverride = env.VITE_GIT_COMMIT?.trim();
  const dirtyOverride = env.VITE_GIT_DIRTY?.trim().toLowerCase();
  const explicitDirty =
    dirtyOverride === "true"
      ? true
      : dirtyOverride === "false"
        ? false
        : undefined;

  if (commitOverride) {
    return {
      commit: commitPattern.test(commitOverride) ? commitOverride : "unknown",
      dirty: explicitDirty ?? false,
    };
  }

  // Without this checkout's marker, Git could discover an unrelated parent repo.
  // A .git file also supports linked worktrees and submodules.
  if (!existsSync(join(repoRoot, ".git"))) {
    return { ...unknownBuild };
  }

  try {
    const git = (...args) =>
      execFileSync("git", args, {
        cwd: repoRoot,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
    const commit = git("rev-parse", "--verify", "HEAD");
    if (!commitPattern.test(commit)) return { ...unknownBuild };

    return {
      commit,
      dirty:
        explicitDirty ??
        Boolean(git("status", "--porcelain", "--untracked-files=no")),
    };
  } catch {
    return { ...unknownBuild };
  }
}
