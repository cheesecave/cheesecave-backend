<!-- src/kohaku-hub-ui/src/App.vue -->
<template>
  <div
    class="app-shell w-full bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100 transition-colors flex flex-col"
  >
    <TheHeader class="shrink-0" />
    <PageScrollArea>
      <main class="flex-1 min-w-0">
        <RouterView v-slot="{ Component, route }">
          <keep-alive :include="['RepoViewer']">
            <component :is="Component" :key="getRouteKey(route)" />
          </keep-alive>
        </RouterView>
      </main>
      <TheFooter />
    </PageScrollArea>
  </div>
</template>

<script setup>
import TheHeader from "@/components/layout/TheHeader.vue";
import TheFooter from "@/components/layout/TheFooter.vue";
import PageScrollArea from "@/components/layout/PageScrollArea.vue";

// Theme is applied early via inline script in index.html to prevent flash
// No need to initialize here

/**
 * Generate unique key for route to control when component should be reused
 * Same repo = same key = reuse component (no flicker)
 * Different repo = different key = new component instance
 */
function getRouteKey(route) {
  // Extract repo identifier from path
  const match = route.path.match(
    /^\/(models|datasets|spaces)\/([^/]+)\/([^/]+)/,
  );
  if (match) {
    const [, type, namespace, name] = match;
    return `${type}-${namespace}-${name}`;
  }
  return route.path;
}
</script>

<style scoped>
.app-shell {
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
}
</style>
