<template>
  <ElScrollbar
    class="page-scroll-area"
    wrap-class="page-scroll-wrap"
    view-class="page-scroll-content"
    :tabindex="0"
    always
    role="region"
    aria-label="Page content"
  >
    <slot />
  </ElScrollbar>
</template>

<script setup>
import { ElScrollbar } from "element-plus";
</script>

<style scoped>
.page-scroll-area {
  flex: 1;
  min-height: 0;
  height: 0;
  width: 100%;
}

:deep(.page-scroll-content) {
  display: flex;
  flex-direction: column;
  min-height: 100%;
}
</style>
