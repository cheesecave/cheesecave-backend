<!-- src/kohaku-hub-ui/src/components/layout/TheFooter.vue -->
<script setup>
import { storeToRefs } from "pinia";
import { useSiteBrandingStore } from "@/stores/siteBranding";

const { branding } = storeToRefs(useSiteBrandingStore());

const buildInfo =
  typeof __BUILD_INFO__ === "undefined"
    ? { commit: "unknown", dirty: false }
    : __BUILD_INFO__;
const versionLabel = `${buildInfo.commit.slice(0, 7)}${buildInfo.dirty ? "-dirty" : ""}`;
const versionTitle =
  buildInfo.commit === "unknown"
    ? "Frontend Git commit unavailable"
    : `Frontend Git commit: ${buildInfo.commit}${buildInfo.dirty ? " (uncommitted changes)" : ""}`;
const commitUrl =
  buildInfo.commit === "unknown"
    ? null
    : `https://github.com/deepghs/KohakuHub/commit/${buildInfo.commit}`;
</script>

<template>
  <footer
    class="bg-gray-100 dark:bg-gray-800 border-t border-gray-200 dark:border-gray-700 mt-4 transition-colors"
  >
    <div class="container-main py-8">
      <div
        class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-8"
      >
        <div>
          <h3 class="font-semibold mb-3 break-words">
            {{ branding.site_name }}
          </h3>
          <p
            class="text-sm text-gray-600 dark:text-gray-400 whitespace-pre-wrap break-words"
          >
            {{ branding.footer_description }}
          </p>
        </div>

        <div>
          <h3 class="font-semibold mb-3">Resources</h3>
          <div class="flex flex-col gap-2 text-sm">
            <a
              href="/docs"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >Documentation</a
            >
            <a
              href="/about"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >About</a
            >
          </div>
        </div>

        <div>
          <h3 class="font-semibold mb-3">Start</h3>
          <div class="flex flex-col gap-2 text-sm">
            <a
              href="/get-started"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >Get started</a
            >
            <a
              href="/self-hosted"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >Self-host</a
            >
          </div>
        </div>

        <div>
          <h3 class="font-semibold mb-3">Community</h3>
          <div class="flex flex-col gap-2 text-sm">
            <a
              href="https://github.com/KohakuBlueleaf/KohakuHub"
              target="_blank"
              rel="noopener noreferrer"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              GitHub
            </a>
            <a
              href="https://discord.gg/xWYrkyvJ2s"
              target="_blank"
              rel="noopener noreferrer"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              Discord
            </a>
            <a
              href="https://github.com/deepghs/KohakuHub"
              target="_blank"
              rel="noopener noreferrer"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              DeepGHS fork
            </a>
            <a
              href="https://github.com/deepghs/KohakuHub/issues"
              target="_blank"
              rel="noopener noreferrer"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              Report an issue
            </a>
          </div>
        </div>

        <div>
          <h3 class="font-semibold mb-3">Legal</h3>
          <div class="flex flex-col gap-2 text-sm">
            <a
              href="/terms"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >Terms</a
            >
            <a
              href="/privacy"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
              >Privacy</a
            >
          </div>
        </div>
      </div>

      <div
        class="mt-8 pt-8 border-t border-gray-300 dark:border-gray-600 text-center text-sm text-gray-600 dark:text-gray-400"
      >
        <p>
          Powered by
          <a
            href="https://github.com/deepghs/KohakuHub"
            target="_blank"
            rel="noopener noreferrer"
            class="font-semibold text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300 underline underline-offset-2 transition-colors"
            >DeepGHS</a
          >
          · Based on KohakuHub
        </p>
        <p class="mt-2">
          © 2025
          <a
            href="https://github.com/KohakuBlueleaf/KohakuHub"
            target="_blank"
            rel="noopener noreferrer"
            class="font-semibold text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300 underline underline-offset-2 transition-colors"
            >KohakuHub</a
          >. Licensed under
          <a
            href="https://github.com/deepghs/KohakuHub/blob/main/LICENSE"
            target="_blank"
            rel="noopener noreferrer"
            class="font-semibold text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300 underline underline-offset-2 transition-colors"
            >AGPL-3.0</a
          >
        </p>
        <div
          class="mt-2 text-xs font-mono"
          data-testid="frontend-version"
          :title="versionTitle"
        >
          Frontend
          <a
            v-if="commitUrl"
            :href="commitUrl"
            :aria-label="versionTitle"
            target="_blank"
            rel="noopener noreferrer"
            class="text-blue-600 dark:text-blue-400 hover:text-blue-500 dark:hover:text-blue-300 underline underline-offset-2 transition-colors"
            >{{ versionLabel }}</a
          >
          <span v-else>{{ versionLabel }}</span>
        </div>
      </div>
    </div>
  </footer>
</template>
