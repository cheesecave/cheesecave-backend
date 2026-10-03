<script setup>
defineProps({
  title: { type: String, default: "" },
  subtitle: { type: String, default: "" },
});
</script>

<template>
  <header class="admin-page-header">
    <div class="admin-page-heading">
      <h1 class="admin-page-title">
        <slot name="title">{{ title }}</slot>
      </h1>
      <p v-if="subtitle || $slots.subtitle" class="admin-page-subtitle">
        <slot name="subtitle">{{ subtitle }}</slot>
      </p>
    </div>
    <div v-if="$slots.actions" class="admin-page-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.admin-page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}

.admin-page-heading {
  flex: 1;
  min-width: 0;
}

.admin-page-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin: 0;
  color: var(--text-primary);
  font-size: 30px;
  font-weight: 700;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.admin-page-subtitle {
  margin: 8px 0 0;
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.admin-page-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  max-width: 50%;
}

.admin-page-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

@container (max-width: 720px) {
  .admin-page-header {
    flex-direction: column;
    gap: 16px;
  }

  .admin-page-actions {
    align-self: flex-end;
    max-width: 100%;
  }
}
</style>
