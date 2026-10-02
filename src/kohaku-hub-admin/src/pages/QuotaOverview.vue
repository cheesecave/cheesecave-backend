<script setup>
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import { useAdminStore } from "@/stores/admin";
import { getQuotaOverview, formatBytes } from "@/utils/api";
import { ElMessage } from "element-plus";

const router = useRouter();
const adminStore = useAdminStore();
const overview = ref(null);
const loading = ref(false);

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadOverview() {
  if (!checkAuth()) return;

  loading.value = true;
  try {
    overview.value = await getQuotaOverview(adminStore.token);
  } catch (error) {
    console.error("Failed to load quota overview:", error);
    if (error.response?.status === 401 || error.response?.status === 403) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to load quota overview",
      );
    }
  } finally {
    loading.value = false;
  }
}

function navigateToUser(username) {
  router.push(`/users`);
  ElMessage.info(`Navigate to user: ${username}`);
}

function navigateToRepo() {
  router.push(`/repositories`);
}

onMounted(() => {
  loadOverview();
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-3xl font-bold text-gray-900 dark:text-gray-100">
          Quota Overview
        </h1>
        <el-button @click="loadOverview" :loading="loading">
          <template #icon>
            <span class="i-carbon-renew" aria-hidden="true" />
          </template>
          Refresh
        </el-button>
      </div>

      <div v-loading="loading">
        <!-- System Storage Summary -->
        <el-card class="mb-6">
          <template #header>
            <div class="flex items-center gap-2">
              <div class="i-carbon-data-volume text-blue-600 text-xl" />
              <span class="font-bold text-lg">System Storage</span>
            </div>
          </template>

          <div v-if="overview" class="storage-summary">
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <div class="stat-box">
                <div class="stat-label">Private Storage</div>
                <div class="stat-value">
                  {{ formatBytes(overview.system_storage.private_used) }}
                </div>
              </div>
              <div class="stat-box">
                <div class="stat-label">Public Storage</div>
                <div class="stat-value">
                  {{ formatBytes(overview.system_storage.public_used) }}
                </div>
              </div>
              <div class="stat-box">
                <div class="stat-label">LFS Storage</div>
                <div class="stat-value">
                  {{ formatBytes(overview.system_storage.lfs_used) }}
                </div>
              </div>
            </div>

            <div class="total-storage">
              <div class="total-storage-label">Total Storage Used</div>
              <div class="total-storage-value">
                {{ formatBytes(overview.system_storage.total_used) }}
              </div>
            </div>
          </div>
        </el-card>

        <!-- Warnings Section -->
        <div
          v-if="
            overview &&
            (overview.users_over_quota.length > 0 ||
              overview.repos_over_quota.length > 0)
          "
          class="mb-6"
        >
          <el-alert type="warning" :closable="false" show-icon class="mb-4">
            <template #title>
              <span class="font-bold">
                {{
                  overview.users_over_quota.length +
                  overview.repos_over_quota.length
                }}
                Warning(s) Detected
              </span>
            </template>
            Some users or repositories have exceeded their storage quotas.
          </el-alert>

          <!-- Users Over Quota -->
          <el-card v-if="overview.users_over_quota.length > 0" class="mb-4">
            <template #header>
              <div class="flex items-center gap-2">
                <div class="i-carbon-warning-alt text-red-600 text-xl" />
                <span class="font-bold">
                  Users Over Quota ({{ overview.users_over_quota.length }})
                </span>
              </div>
            </template>

            <el-table :data="overview.users_over_quota" stripe>
              <el-table-column prop="username" label="Username" width="180">
                <template #default="{ row }">
                  <el-button type="text" @click="navigateToUser(row.username)">
                    {{ row.username }}
                  </el-button>
                </template>
              </el-table-column>

              <el-table-column label="Private Usage" width="200">
                <template #default="{ row }">
                  <div class="quota-cell">
                    <span
                      :class="{
                        'text-red-600 font-semibold':
                          row.private_percentage > 100,
                        'text-orange-600': row.private_percentage > 90,
                      }"
                    >
                      {{ row.private_percentage }}%
                    </span>
                    <div class="text-xs text-gray-500">
                      {{ formatBytes(row.private_used) }} /
                      {{ formatBytes(row.private_quota) }}
                    </div>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="Public Usage" width="200">
                <template #default="{ row }">
                  <div class="quota-cell">
                    <span
                      :class="{
                        'text-red-600 font-semibold':
                          row.public_percentage > 100,
                        'text-orange-600': row.public_percentage > 90,
                      }"
                    >
                      {{ row.public_percentage }}%
                    </span>
                    <div class="text-xs text-gray-500">
                      {{ formatBytes(row.public_used) }} /
                      {{ formatBytes(row.public_quota) }}
                    </div>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="Action" width="150" align="right">
                <template #default="{ row }">
                  <el-button
                    type="primary"
                    size="small"
                    @click="navigateToUser(row.username)"
                  >
                    Manage
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>

          <!-- Repositories Over Quota -->
          <el-card v-if="overview.repos_over_quota.length > 0" class="mb-4">
            <template #header>
              <div class="flex items-center gap-2">
                <div class="i-carbon-warning-alt text-orange-600 text-xl" />
                <span class="font-bold">
                  Repositories Over Quota ({{
                    overview.repos_over_quota.length
                  }})
                </span>
              </div>
            </template>

            <el-table :data="overview.repos_over_quota" stripe>
              <el-table-column label="Repository" min-width="250">
                <template #default="{ row }">
                  <div class="flex items-center gap-2">
                    <el-tag
                      :type="
                        row.repo_type === 'model'
                          ? 'primary'
                          : row.repo_type === 'dataset'
                            ? 'success'
                            : 'warning'
                      "
                      size="small"
                    >
                      {{ row.repo_type }}
                    </el-tag>
                    <span class="font-mono text-sm">{{ row.full_id }}</span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="Usage" width="200">
                <template #default="{ row }">
                  <div class="quota-cell">
                    <span class="text-red-600 font-semibold">
                      {{ row.percentage }}%
                    </span>
                    <div class="text-xs text-gray-500">
                      {{ formatBytes(row.used_bytes) }} /
                      {{ formatBytes(row.quota_bytes) }}
                    </div>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="Action" width="150" align="right">
                <template #default="{ row }">
                  <el-button
                    type="primary"
                    size="small"
                    @click="navigateToRepo()"
                  >
                    View
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>

        <!-- Top Storage Consumers -->
        <el-card class="mb-6">
          <template #header>
            <div class="flex items-center gap-2">
              <div class="i-carbon-chart-bar text-purple-600 text-xl" />
              <span class="font-bold">Top Storage Consumers</span>
            </div>
          </template>

          <el-table
            v-if="overview"
            :data="overview.top_consumers"
            stripe
            max-height="400"
          >
            <el-table-column label="Rank" width="80" align="center">
              <template #default="{ $index }">
                <el-tag
                  :type="$index === 0 ? 'warning' : $index === 1 ? 'info' : ''"
                  size="small"
                >
                  #{{ $index + 1 }}
                </el-tag>
              </template>
            </el-table-column>

            <el-table-column label="Name" min-width="200">
              <template #default="{ row }">
                <div class="flex items-center gap-2">
                  <div
                    :class="
                      row.is_org
                        ? 'i-carbon-enterprise text-purple-600'
                        : 'i-carbon-user text-blue-600'
                    "
                  />
                  <span class="font-semibold">{{ row.username }}</span>
                  <el-tag
                    v-if="row.is_org"
                    type="info"
                    size="small"
                    effect="plain"
                  >
                    Organization
                  </el-tag>
                </div>
              </template>
            </el-table-column>

            <el-table-column label="Total Storage" width="180" align="right">
              <template #default="{ row }">
                <span class="font-mono font-semibold">
                  {{ formatBytes(row.total_bytes) }}
                </span>
              </template>
            </el-table-column>

            <el-table-column label="Action" width="120" align="right">
              <template #default="{ row }">
                <el-button size="small" @click="navigateToUser(row.username)">
                  View
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- No Warnings Message -->
        <el-result
          v-if="
            overview &&
            overview.users_over_quota.length === 0 &&
            overview.repos_over_quota.length === 0
          "
          icon="success"
          title="All Clear!"
          sub-title="No users or repositories are over their storage quotas."
          class="mb-6"
        />
      </div>
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.storage-summary {
  padding: 12px 0;
}

.stat-box {
  padding: 20px;
  background: linear-gradient(135deg, var(--bg-hover) 0%, var(--bg-card) 100%);
  border-radius: 12px;
  text-align: center;
  border: 1px solid var(--border-default);
  transition: all 0.3s ease;
}

.stat-box:hover {
  border-color: var(--color-info);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.stat-label {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
  color: var(--text-primary);
  font-family: "SF Mono", "Monaco", "Consolas", monospace;
}

.total-storage {
  padding: 28px 24px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 12px;
  text-align: center;
  box-shadow: var(--shadow-lg);
  margin-top: 16px;
}

.total-storage-label {
  color: rgba(255, 255, 255, 0.95);
  font-size: 14px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 12px;
}

.total-storage-value {
  color: #ffffff !important;
  font-size: 36px;
  font-weight: 800;
  font-family: "SF Mono", "Monaco", "Consolas", monospace;
  text-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}

.quota-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.quota-cell .text-red-600 {
  font-weight: 700;
}

.quota-cell .text-orange-600 {
  font-weight: 600;
}
</style>
