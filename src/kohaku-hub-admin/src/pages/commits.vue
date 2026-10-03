<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import AdminPage from "@/components/AdminPage.vue";
import AdminPageHeader from "@/components/AdminPageHeader.vue";
import { useAdminStore } from "@/stores/admin";
import { listCommits } from "@/utils/api";
import { ElMessage } from "element-plus";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();
const commits = ref([]);
const loading = ref(false);

// Filters
const filterRepoId = ref("");
const filterUsername = ref("");

// Pagination
const currentPage = ref(1);
const pageSize = ref(20);

// Sorting
const sortBy = ref("created_at");
const sortOrder = ref("descending");

// Computed sorted commits
const sortedCommits = computed(() => {
  if (!commits.value || commits.value.length === 0) return [];

  const sorted = [...commits.value];
  sorted.sort((a, b) => {
    let aVal, bVal;

    switch (sortBy.value) {
      case "id":
        aVal = a.id;
        bVal = b.id;
        break;
      case "created_at":
        aVal = new Date(a.created_at).getTime();
        bVal = new Date(b.created_at).getTime();
        break;
      case "username":
        aVal = a.username?.toLowerCase() || "";
        bVal = b.username?.toLowerCase() || "";
        break;
      case "repo_full_id":
        aVal = a.repo_full_id?.toLowerCase() || "";
        bVal = b.repo_full_id?.toLowerCase() || "";
        break;
      default:
        aVal = a[sortBy.value];
        bVal = b[sortBy.value];
    }

    if (sortOrder.value === "ascending") {
      return aVal > bVal ? 1 : aVal < bVal ? -1 : 0;
    } else {
      return aVal < bVal ? 1 : aVal > bVal ? -1 : 0;
    }
  });

  return sorted;
});

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadCommits() {
  if (!checkAuth()) return;

  loading.value = true;
  try {
    const response = await listCommits(adminStore.token, {
      repo_full_id: filterRepoId.value || undefined,
      username: filterUsername.value || undefined,
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    });
    commits.value = response.commits;
  } catch (error) {
    console.error("Failed to load commits:", error);
    if (error.response?.status === 401 || error.response?.status === 403) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to load commits",
      );
    }
  } finally {
    loading.value = false;
  }
}

function formatDate(dateStr) {
  return dayjs(dateStr).format("YYYY-MM-DD HH:mm:ss");
}

function handleSortChange({ prop, order }) {
  if (!prop || !order) {
    sortBy.value = "created_at";
    sortOrder.value = "descending";
    return;
  }
  sortBy.value = prop;
  sortOrder.value = order;
}

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

function truncateCommitId(commitId) {
  return commitId?.substring(0, 8) || "";
}

function truncateMessage(message, maxLength = 100) {
  if (!message) return "";
  return message.length > maxLength
    ? message.substring(0, maxLength) + "..."
    : message;
}

function applyFilters() {
  currentPage.value = 1;
  loadCommits();
}

function resetFilters() {
  filterRepoId.value = "";
  filterUsername.value = "";
  currentPage.value = 1;
  loadCommits();
}

onMounted(() => {
  loadCommits();
});
</script>

<template>
  <AdminLayout>
    <AdminPage>
      <AdminPageHeader
        title="Commit History"
        subtitle="Browse repository commits and filter activity by repository or author."
      />

      <!-- Filters -->
      <el-card class="mb-4">
        <div class="flex gap-4 items-end">
          <el-form-item label="Repository ID" class="mb-0">
            <el-input
              v-model="filterRepoId"
              placeholder="e.g., org/repo-name"
              clearable
              style="width: 250px"
            />
          </el-form-item>

          <el-form-item label="Author" class="mb-0">
            <el-input
              v-model="filterUsername"
              placeholder="Filter by username"
              clearable
              style="width: 200px"
            />
          </el-form-item>

          <el-button type="primary" @click="applyFilters"
            >Apply Filters</el-button
          >
          <el-button @click="resetFilters">Reset</el-button>
        </div>
      </el-card>

      <!-- Commits Table -->
      <el-card>
        <el-empty
          v-if="!loading && commits.length === 0"
          description="No commits found"
        />
        <el-table
          v-else
          :data="sortedCommits"
          v-loading="loading"
          stripe
          @sort-change="handleSortChange"
          :default-sort="{ prop: 'created_at', order: 'descending' }"
        >
          <el-table-column prop="id" label="ID" width="80" sortable="custom" />
          <el-table-column label="Commit SHA" width="120">
            <template #default="{ row }">
              <code
                class="text-xs font-mono text-gray-600 dark:text-gray-400"
                >{{ truncateCommitId(row.commit_id) }}</code
              >
            </template>
          </el-table-column>
          <el-table-column
            prop="repo_full_id"
            label="Repository"
            min-width="200"
            sortable="custom"
          >
            <template #default="{ row }">
              <div class="flex items-center gap-2">
                <el-tag :type="getRepoTypeColor(row.repo_type)" size="small">
                  {{ row.repo_type }}
                </el-tag>
                <span class="font-mono text-sm">{{ row.repo_full_id }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="branch" label="Branch" width="120" />
          <el-table-column
            prop="username"
            label="Author"
            width="150"
            sortable="custom"
          />
          <el-table-column label="Message" min-width="250">
            <template #default="{ row }">
              <div class="text-sm" :title="row.message">
                {{ truncateMessage(row.message) }}
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="created_at"
            label="Created"
            width="180"
            sortable="custom"
          >
            <template #default="{ row }">
              {{ formatDate(row.created_at) }}
            </template>
          </el-table-column>
        </el-table>

        <!-- Pagination -->
        <div class="mt-4 flex justify-center">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50, 100]"
            layout="sizes, prev, pager, next"
            :total="commits.length"
            @current-change="loadCommits"
            @size-change="loadCommits"
          />
        </div>
      </el-card>
    </AdminPage>
  </AdminLayout>
</template>

<style scoped>
:deep(.el-card) {
  background-color: var(--bg-card);
  border-color: var(--border-default);
  border-radius: 12px;
}

:deep(.el-table) {
  background-color: var(--bg-card);
}

:deep(.el-table th) {
  background-color: var(--bg-hover);
  color: var(--text-primary);
  font-weight: 600;
}

:deep(.el-table td) {
  color: var(--text-primary);
}

:deep(.el-table__body tr:hover > td) {
  background-color: var(--bg-hover) !important;
}
</style>
