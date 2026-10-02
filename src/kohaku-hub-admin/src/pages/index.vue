<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import StatsCard from "@/components/StatsCard.vue";
import ChartCard from "@/components/ChartCard.vue";
import { useAdminStore } from "@/stores/admin";
import {
  getDetailedStats,
  getTopRepositories,
  getTimeseriesStats,
  getQuotaOverview,
} from "@/utils/api";
import { formatBytes } from "@/utils/api";
import { ElMessage } from "element-plus";

const router = useRouter();
const adminStore = useAdminStore();
const stats = ref(null);
const topRepos = ref([]);
const loading = ref(false);
const timeseriesData = ref(null);
const chartDays = ref(30);
const quotaOverview = ref(null);

const hasData = computed(() => stats.value !== null);

async function loadStats() {
  if (!adminStore.token) {
    router.push("/login");
    return;
  }

  loading.value = true;
  try {
    const [statsData, topReposData, timeseriesResult, quotaData] =
      await Promise.all([
        getDetailedStats(adminStore.token),
        getTopRepositories(adminStore.token, 5, "commits"),
        getTimeseriesStats(adminStore.token, chartDays.value),
        getQuotaOverview(adminStore.token),
      ]);

    stats.value = statsData;
    topRepos.value = topReposData.top_repositories;
    timeseriesData.value = timeseriesResult;
    quotaOverview.value = quotaData;
  } catch (error) {
    console.error("Failed to load stats:", error);
    if (error.response?.status === 401 || error.response?.status === 403) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to load system stats",
      );
    }
  } finally {
    loading.value = false;
  }
}

// Computed chart data for user growth
const userGrowthChart = computed(() => {
  if (!timeseriesData.value) return null;

  const dates = Object.keys(timeseriesData.value.users_by_day).sort();
  const values = dates.map((date) => timeseriesData.value.users_by_day[date]);

  return {
    labels: dates,
    datasets: [
      {
        label: "New Users",
        data: values,
        borderColor: "#409EFF",
        backgroundColor: "rgba(64, 158, 255, 0.1)",
        fill: true,
        tension: 0.4,
      },
    ],
  };
});

// Computed chart data for repository growth
const repoGrowthChart = computed(() => {
  if (!timeseriesData.value) return null;

  const dates = Object.keys(timeseriesData.value.repositories_by_day).sort();
  const modelData = dates.map(
    (date) => timeseriesData.value.repositories_by_day[date]?.model || 0,
  );
  const datasetData = dates.map(
    (date) => timeseriesData.value.repositories_by_day[date]?.dataset || 0,
  );
  const spaceData = dates.map(
    (date) => timeseriesData.value.repositories_by_day[date]?.space || 0,
  );

  return {
    labels: dates,
    datasets: [
      {
        label: "Models",
        data: modelData,
        borderColor: "#409EFF",
        backgroundColor: "rgba(64, 158, 255, 0.1)",
        fill: true,
      },
      {
        label: "Datasets",
        data: datasetData,
        borderColor: "#67C23A",
        backgroundColor: "rgba(103, 194, 58, 0.1)",
        fill: true,
      },
      {
        label: "Spaces",
        data: spaceData,
        borderColor: "#E6A23C",
        backgroundColor: "rgba(230, 162, 60, 0.1)",
        fill: true,
      },
    ],
  };
});

// Computed chart data for commit activity
const commitActivityChart = computed(() => {
  if (!timeseriesData.value) return null;

  const dates = Object.keys(timeseriesData.value.commits_by_day).sort();
  const values = dates.map((date) => timeseriesData.value.commits_by_day[date]);

  return {
    labels: dates,
    datasets: [
      {
        label: "Commits",
        data: values,
        borderColor: "#F56C6C",
        backgroundColor: "rgba(245, 108, 108, 0.1)",
        fill: true,
        tension: 0.4,
      },
    ],
  };
});

function getRepoTypeColor(type) {
  switch (type) {
    case "model":
      return "primary";
    case "dataset":
      return "success";
    case "space":
      return "warning";
    default:
      return "info";
  }
}

onMounted(() => {
  loadStats();
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <h1 class="text-3xl font-bold mb-6 text-gray-900 dark:text-gray-100">
        Dashboard
      </h1>

      <div v-loading="loading" class="stats-grid">
        <StatsCard
          title="Total Users"
          :value="stats?.users?.total || 0"
          :subtitle="`Active: ${stats?.users?.active || 0} | Verified: ${stats?.users?.verified || 0}`"
          icon="i-carbon-user-multiple"
          color="blue"
        />

        <StatsCard
          title="Organizations"
          :value="stats?.organizations?.total || 0"
          icon="i-carbon-enterprise"
          color="purple"
        />

        <StatsCard
          title="Total Repositories"
          :value="stats?.repositories?.total || 0"
          :subtitle="`Models: ${stats?.repositories?.by_type?.model || 0} | Datasets: ${stats?.repositories?.by_type?.dataset || 0}`"
          icon="i-carbon-data-base"
          color="green"
        />

        <StatsCard
          title="Total Commits"
          :value="stats?.commits?.total || 0"
          icon="i-carbon-version"
          color="orange"
        />

        <StatsCard
          title="Total Storage"
          :value="formatBytes(stats?.storage?.total_used || 0)"
          :subtitle="`Private: ${formatBytes(stats?.storage?.private_used || 0)} | Public: ${formatBytes(stats?.storage?.public_used || 0)}`"
          icon="i-carbon-data-volume"
          color="cyan"
        />

        <StatsCard
          title="LFS Objects"
          :value="stats?.lfs?.total_objects || 0"
          :subtitle="formatBytes(stats?.lfs?.total_size || 0)"
          icon="i-carbon-document"
          color="pink"
        />
      </div>

      <!-- Activity Charts -->
      <div v-if="timeseriesData" class="charts-grid mb-6">
        <ChartCard
          v-if="userGrowthChart"
          title="User Growth (Last 30 Days)"
          :labels="userGrowthChart.labels"
          :datasets="userGrowthChart.datasets"
          :height="220"
        />

        <ChartCard
          v-if="repoGrowthChart"
          title="Repository Creation (Last 30 Days)"
          :labels="repoGrowthChart.labels"
          :datasets="repoGrowthChart.datasets"
          :height="220"
        />

        <ChartCard
          v-if="commitActivityChart"
          title="Commit Activity (Last 30 Days)"
          :labels="commitActivityChart.labels"
          :datasets="commitActivityChart.datasets"
          :height="220"
        />
      </div>

      <!-- Quota Warnings -->
      <el-card
        v-if="
          quotaOverview &&
          (quotaOverview.users_over_quota.length > 0 ||
            quotaOverview.repos_over_quota.length > 0)
        "
        class="mb-6"
      >
        <template #header>
          <div class="flex items-center gap-2">
            <div class="i-carbon-warning text-orange-600" />
            <span class="font-bold">Quota Warnings</span>
            <el-tag type="warning" size="small">
              {{
                quotaOverview.users_over_quota.length +
                quotaOverview.repos_over_quota.length
              }}
            </el-tag>
          </div>
        </template>

        <div v-if="quotaOverview.users_over_quota.length > 0" class="mb-4">
          <h3 class="text-sm font-semibold mb-2 text-red-600">
            Users Over Quota ({{ quotaOverview.users_over_quota.length }})
          </h3>
          <el-table
            :data="quotaOverview.users_over_quota.slice(0, 5)"
            size="small"
            stripe
          >
            <el-table-column prop="username" label="Username" width="150" />
            <el-table-column label="Private" width="150" align="right">
              <template #default="{ row }">
                <span
                  :class="{
                    'text-red-600 font-semibold': row.private_percentage > 100,
                  }"
                >
                  {{ row.private_percentage }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="Public" width="150" align="right">
              <template #default="{ row }">
                <span
                  :class="{
                    'text-red-600 font-semibold': row.public_percentage > 100,
                  }"
                >
                  {{ row.public_percentage }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="Action" width="150" align="right">
              <template #default="{ row }">
                <el-button
                  type="primary"
                  size="small"
                  @click="$router.push(`/users`)"
                >
                  Manage
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div v-if="quotaOverview.repos_over_quota.length > 0">
          <h3 class="text-sm font-semibold mb-2 text-red-600">
            Repositories Over Quota ({{
              quotaOverview.repos_over_quota.length
            }})
          </h3>
          <el-table
            :data="quotaOverview.repos_over_quota.slice(0, 5)"
            size="small"
            stripe
          >
            <el-table-column label="Repository" min-width="200">
              <template #default="{ row }">
                <el-tag :type="getRepoTypeColor(row.repo_type)" size="small">
                  {{ row.repo_type }}
                </el-tag>
                <span class="ml-2 font-mono text-sm">{{ row.full_id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="Usage" width="120" align="right">
              <template #default="{ row }">
                <span class="text-red-600 font-semibold">
                  {{ row.percentage }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column label="Size" width="120" align="right">
              <template #default="{ row }">
                {{ formatBytes(row.used_bytes) }}
              </template>
            </el-table-column>
            <el-table-column label="Action" width="150" align="right">
              <template #default="{ row }">
                <el-button
                  type="primary"
                  size="small"
                  @click="$router.push(`/repositories`)"
                >
                  View
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-card>

      <!-- Top Contributors -->
      <el-card class="mb-6" v-if="stats?.commits?.top_contributors?.length > 0">
        <template #header>
          <span class="font-bold">Top Contributors</span>
        </template>
        <el-table :data="stats.commits.top_contributors" stripe>
          <el-table-column prop="username" label="Username" />
          <el-table-column label="Commits" width="120" align="right">
            <template #default="{ row }">
              <el-tag type="success">{{ row.commit_count }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- Top Repositories by Commits -->
      <el-card class="mb-6" v-if="topRepos.length > 0">
        <template #header>
          <span class="font-bold">Top Repositories by Commits</span>
        </template>
        <el-table :data="topRepos" stripe>
          <el-table-column label="Repository" min-width="250">
            <template #default="{ row }">
              <div class="flex items-center gap-2">
                <el-tag :type="getRepoTypeColor(row.repo_type)" size="small">
                  {{ row.repo_type }}
                </el-tag>
                <span class="font-mono">{{ row.repo_full_id }}</span>
                <el-tag
                  v-if="row.private"
                  type="warning"
                  size="small"
                  effect="plain"
                >
                  Private
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="Commits" width="120" align="right">
            <template #default="{ row }">
              <el-tag type="success">{{ row.commit_count }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- Quick Actions -->
      <div class="mt-8">
        <h2 class="text-2xl font-bold mb-4 text-gray-900 dark:text-gray-100">
          Quick Actions
        </h2>
        <div class="flex gap-4 flex-wrap">
          <el-button type="primary" @click="$router.push('/users')">
            <template #icon>
              <span class="i-carbon-user-multiple" aria-hidden="true" />
            </template>
            Manage Users
          </el-button>
          <el-button type="success" @click="$router.push('/repositories')">
            <template #icon>
              <span class="i-carbon-data-base" aria-hidden="true" />
            </template>
            View Repositories
          </el-button>
          <el-button @click="$router.push('/commits')">
            <template #icon>
              <span class="i-carbon-commit" aria-hidden="true" />
            </template>
            View Commits
          </el-button>
          <el-button @click="$router.push('/storage')">
            <template #icon>
              <span class="i-carbon-data-volume" aria-hidden="true" />
            </template>
            Browse Storage
          </el-button>
          <el-button @click="$router.push('/quotas')">
            <template #icon>
              <span class="i-carbon-meter" aria-hidden="true" />
            </template>
            Manage Quotas
          </el-button>
          <el-button @click="loadStats">
            <template #icon>
              <span class="i-carbon-renew" aria-hidden="true" />
            </template>
            Refresh Stats
          </el-button>
        </div>
      </div>
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(250px, 100%), 1fr));
  grid-auto-rows: 1fr;
  gap: 24px;
  margin-bottom: 32px;
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
  gap: 24px;
}

/* Enhanced card styling */
:deep(.el-card) {
  background-color: var(--bg-card);
  border-color: var(--border-default);
  transition: all 0.3s ease;
  border-radius: 12px;
}

:deep(.el-card:hover) {
  box-shadow: var(--shadow-md);
  border-color: var(--border-strong);
}

:deep(.el-card__header) {
  background-color: var(--bg-hover);
  border-bottom-color: var(--border-default);
  padding: 16px 20px;
}

:deep(.el-card__header .font-bold) {
  color: var(--text-primary);
  font-size: 15px;
}

/* Table styling */
:deep(.el-table) {
  background-color: var(--bg-card);
}

:deep(.el-table th) {
  background-color: var(--bg-hover);
  color: var(--text-primary);
  font-weight: 600;
  border-color: var(--border-default);
}

:deep(.el-table td) {
  color: var(--text-primary);
  border-color: var(--border-light);
}

:deep(.el-table__body tr:hover > td) {
  background-color: var(--bg-hover) !important;
}

/* Warning colors */
:deep(.text-red-600) {
  color: var(--color-error) !important;
  font-weight: 700;
}

:deep(.text-sm.text-gray-500) {
  color: var(--text-secondary);
}
</style>
