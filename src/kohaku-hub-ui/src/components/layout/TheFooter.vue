<!-- src/kohaku-hub-ui/src/components/layout/TheFooter.vue -->
<script setup>
import { storeToRefs } from "pinia";
import { useSiteBrandingStore } from "@/stores/siteBranding";

const { branding } = storeToRefs(useSiteBrandingStore());
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
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              GitHub
            </a>
            <a
              href="https://discord.gg/xWYrkyvJ2s"
              target="_blank"
              class="text-gray-600 dark:text-gray-400 hover:text-blue-500 dark:hover:text-blue-400 transition-colors"
            >
              Discord
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
        © 2025 KohakuHub. Licensed under AGPL-3.0
      </div>
    </div>
  </footer>
</template>
