<template>
  <div class="admin-page">
    <slot />
  </div>
</template>

<style scoped>
.admin-page {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 24px;
  color: var(--text-primary);
  container-type: inline-size;
}

@media (max-width: 768px) {
  .admin-page {
    padding: 16px;
  }
}
</style>
