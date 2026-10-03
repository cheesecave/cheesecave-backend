<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import AdminPage from "@/components/AdminPage.vue";
import AdminPageHeader from "@/components/AdminPageHeader.vue";
import { useAdminStore } from "@/stores/admin";
import { getDependencyHealth } from "@/utils/api";
import { ElMessage } from "element-plus";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();

const loading = ref(false);
const report = ref(null);
const lastError = ref(null);
const refreshIntervalSeconds = ref(0);
let refreshTimer = null;

const dependencies = computed(() => report.value?.dependencies ?? []);
const overallStatus = computed(() => report.value?.overall_status ?? "unknown");

const REFRESH_OPTIONS = [
  { label: "Off", value: 0 },
  { label: "Every 30 s", value: 30 },
  { label: "Every 60 s", value: 60 },
  { label: "Every 5 min", value: 300 },
];

const STATUS_TAG_TYPE = {
  ok: "success",
  down: "danger",
  disabled: "info",
  unknown: "warning",
};

const STATUS_LABEL = {
  ok: "OK",
  down: "Down",
  disabled: "Disabled",
  degraded: "Degraded",
  unknown: "Unknown",
};

// Whether KohakuHub supports this LakeFS release (docs/deployment/lakefs.md)
const COMPAT_TAG_TYPE = {
  supported: "success",
  unsupported: "danger",
  untested: "warning",
  unknown: "info",
};

const COMPAT_LABEL = {
  supported: "Supported",
  unsupported: "Unsupported",
  untested: "Untested",
  unknown: "Unknown",
};

const DEPENDENCY_LABEL = {
  postgres: "PostgreSQL",
  minio: "MinIO / S3",
  lakefs: "LakeFS",
  redis: "Redis",
  smtp: "SMTP",
};

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadHealth({ silent = false } = {}) {
  if (!checkAuth()) return;
  loading.value = true;
  try {
    const data = await getDependencyHealth(adminStore.token);
    report.value = data;
    lastError.value = null;
  } catch (error) {
    if (
      error.response?.status === 401 ||
      error.response?.status === 403
    ) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
      return;
    }
    const detail =
      error.response?.data?.detail?.error ||
      error.message ||
      "Failed to load dependency health";
    lastError.value = detail;
    if (!silent) {
      ElMessage.error(detail);
    }
  } finally {
    loading.value = false;
  }
}

function formatLatency(value) {
  if (value === null || value === undefined) return "—";
  return `${value} ms`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}

function statusType(status) {
  return STATUS_TAG_TYPE[status] || "warning";
}

function statusLabel(status) {
  return STATUS_LABEL[status] || status;
}

function dependencyLabel(name) {
  return DEPENDENCY_LABEL[name] || name;
}

function clearTimer() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

watch(refreshIntervalSeconds, (seconds) => {
  clearTimer();
  if (seconds > 0) {
    refreshTimer = setInterval(() => {
      loadHealth({ silent: true });
    }, seconds * 1000);
  }
});

onMounted(() => {
  loadHealth();
});

onBeforeUnmount(() => {
  clearTimer();
});
</script>

<template>
  <AdminLayout>
    <AdminPage>
      <AdminPageHeader title="Dependency Health">
        <template #subtitle
          >Live probes for the services this hub depends on. Useful for quickly
          answering "is the deployment healthy?" without leaving the admin
          UI.</template
        >
        <template #actions>
          <el-select
            v-model="refreshIntervalSeconds"
            class="refresh-select"
            placeholder="Auto-refresh"
          >
            <el-option
              v-for="option in REFRESH_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <el-button
            type="primary"
            :loading="loading"
            @click="loadHealth()"
            data-testid="health-recheck"
          >
            <div class="i-carbon-renew mr-1" />
            Re-check
          </el-button>
        </template>
      </AdminPageHeader>

      <el-alert
        v-if="lastError"
        type="warning"
        :title="lastError"
        :closable="false"
        show-icon
        class="mb-4"
      />

      <el-card
        v-if="report"
        shadow="never"
        class="mb-4 overall-banner"
        data-testid="health-overall"
      >
        <div class="flex items-center gap-3 flex-wrap">
          <span class="font-semibold text-gray-700 dark:text-gray-200">
            Overall
          </span>
          <el-tag :type="statusType(overallStatus)" size="large" effect="dark">
            {{ statusLabel(overallStatus) }}
          </el-tag>
          <span class="text-gray-500 dark:text-gray-400 text-sm">
            checked at {{ formatTimestamp(report.checked_at_ms) }} · probes
            ran in {{ formatLatency(report.elapsed_ms) }} · per-probe timeout
            {{ report.timeout_seconds }} s
          </span>
        </div>
      </el-card>

      <div v-loading="loading" class="cards-grid" data-testid="health-grid">
        <el-card
          v-for="dep in dependencies"
          :key="dep.name"
          shadow="hover"
          class="dep-card"
          :data-testid="`health-card-${dep.name}`"
        >
          <template #header>
            <div class="flex justify-between items-center">
              <span class="font-semibold text-gray-900 dark:text-gray-100">
                {{ dependencyLabel(dep.name) }}
              </span>
              <el-tag :type="statusType(dep.status)" effect="light">
                {{ statusLabel(dep.status) }}
              </el-tag>
            </div>
          </template>
          <ul class="dep-meta">
            <li>
              <span class="meta-label">Latency</span>
              <span class="meta-value">{{ formatLatency(dep.latency_ms) }}</span>
            </li>
            <li>
              <span class="meta-label">Version</span>
              <span class="meta-value">{{ dep.version || "—" }}</span>
            </li>
            <li>
              <span class="meta-label">Endpoint</span>
              <span class="meta-value endpoint" :title="dep.endpoint || ''">
                {{ dep.endpoint || "—" }}
              </span>
            </li>
            <li
              v-if="dep.compatibility"
              :data-testid="`health-compat-${dep.name}`"
            >
              <span class="meta-label">Compatibility</span>
              <span class="meta-value">
                <el-tag
                  :type="COMPAT_TAG_TYPE[dep.compatibility.status] || 'info'"
                  size="small"
                  :title="dep.compatibility.message"
                >
                  {{ COMPAT_LABEL[dep.compatibility.status] || "Unknown" }}
                </el-tag>
                <el-tag
                  v-if="dep.compatibility.license === 'bsl-1.1'"
                  type="warning"
                  size="small"
                  class="ml-1"
                  title="LakeFS 1.87.0 and later use the Business Source License 1.1, not Apache 2.0"
                >
                  BSL 1.1
                </el-tag>
              </span>
            </li>
            <li
              v-if="dep.compatibility && dep.compatibility.status !== 'supported'"
            >
              <span class="meta-label">Note</span>
              <span class="meta-note">{{ dep.compatibility.message }}</span>
            </li>
            <li v-if="dep.detail">
              <span class="meta-label">Detail</span>
              <span class="meta-value">{{ dep.detail }}</span>
            </li>
          </ul>
        </el-card>

        <el-empty
          v-if="!loading && !dependencies.length"
          description="No probe results yet"
        />
      </div>
    </AdminPage>
  </AdminLayout>
</template>

<style scoped>
.refresh-select {
  width: 160px;
}

.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  min-height: 120px;
}

.dep-card {
  border-radius: 10px;
}

.dep-meta {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}

.dep-meta li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
}

.meta-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  flex-shrink: 0;
}

.meta-value {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  text-align: right;
  word-break: break-all;
}

.meta-note {
  font-size: 13px;
  text-align: right;
}

.meta-value.endpoint {
  max-width: 60%;
}
</style>
