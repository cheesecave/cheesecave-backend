<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import FileTree from "@/components/FileTree.vue";
import { useAdminStore } from "@/stores/admin";
import {
  listRepositories,
  getRepositoryDetails,
  getRepositoryStorageBreakdown,
  recalculateAllRepoStorage,
  listCommits,
  deleteRepositoryAdmin,
  moveRepositoryAdmin,
  squashRepositoryAdmin,
  getSiteConfig,
} from "@/utils/api";
import { formatBytes } from "@/utils/api";
import { getRepositoryOperationCapabilities } from "@/utils/repositoryOperationCapabilities";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();
const repositories = ref([]);
const totalCount = ref(0);
const loading = ref(false);
const recalculating = ref(false);
const selectedRepo = ref(null);
const repoDialogVisible = ref(false);
const activeTab = ref("overview");
const storageBreakdown = ref(null);
const repoCommits = ref([]);
const loadingStorage = ref(false);
const loadingCommits = ref(false);
const squashEnabled = ref(false);

// Actions
const actionLoading = ref(false);
const moveForm = ref({ toNamespace: "", toName: "" });

// Search
const searchQuery = ref("");
const searchDebounceTimer = ref(null);

// Filters
const filterRepoType = ref("");
const filterNamespace = ref("");

// Pagination
const currentPage = ref(1);
const pageSize = ref(20);

// Sorting
const sortBy = ref("id");
const sortOrder = ref("descending");

// Computed sorted repositories
const sortedRepos = computed(() => {
  if (!repositories.value || repositories.value.length === 0) return [];

  const sorted = [...repositories.value];
  sorted.sort((a, b) => {
    let aVal, bVal;

    switch (sortBy.value) {
      case "id":
        aVal = a.id;
        bVal = b.id;
        break;
      case "full_id":
        aVal = a.full_id?.toLowerCase() || "";
        bVal = b.full_id?.toLowerCase() || "";
        break;
      case "owner_username":
        aVal = a.owner_username?.toLowerCase() || "";
        bVal = b.owner_username?.toLowerCase() || "";
        break;
      case "created_at":
        aVal = new Date(a.created_at).getTime();
        bVal = new Date(b.created_at).getTime();
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

async function loadRepositories() {
  if (!checkAuth()) return;

  loading.value = true;
  try {
    const response = await listRepositories(adminStore.token, {
      search: searchQuery.value || undefined,
      repo_type: filterRepoType.value || undefined,
      namespace: filterNamespace.value || undefined,
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    });
    repositories.value = response.repositories;
    totalCount.value = response.total || 0;
  } catch (error) {
    console.error("Failed to load repositories:", error);
    if (error.response?.status === 401 || error.response?.status === 403) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to load repositories",
      );
    }
  } finally {
    loading.value = false;
  }
}

async function loadOperationCapabilities() {
  try {
    const siteConfig = await getSiteConfig();
    squashEnabled.value = getRepositoryOperationCapabilities(siteConfig).squash;
  } catch (err) {
    // Keep Squash hidden when capabilities cannot be established.
    console.warn("Failed to load repository operation capabilities:", err);
  }
}

function handleSearchInput() {
  // Clear existing timer
  if (searchDebounceTimer.value) {
    clearTimeout(searchDebounceTimer.value);
  }

  // Set new timer - wait 500ms after last input before searching
  searchDebounceTimer.value = setTimeout(() => {
    currentPage.value = 1; // Reset to first page when searching
    loadRepositories();
  }, 500);
}

function clearSearch() {
  searchQuery.value = "";
  loadRepositories();
}

async function handleViewRepo(row) {
  if (!checkAuth()) return;

  try {
    selectedRepo.value = await getRepositoryDetails(
      adminStore.token,
      row.repo_type,
      row.namespace,
      row.name,
    );
    activeTab.value = "overview";

    // Reset data from previous repo
    storageBreakdown.value = null;
    repoCommits.value = [];

    repoDialogVisible.value = true;

    // Load storage breakdown in background
    loadStorageBreakdown();
  } catch (error) {
    ElMessage.error(
      error.response?.data?.detail?.error ||
        "Failed to load repository details",
    );
  }
}

async function loadStorageBreakdown() {
  if (!selectedRepo.value) return;

  loadingStorage.value = true;
  try {
    storageBreakdown.value = await getRepositoryStorageBreakdown(
      adminStore.token,
      selectedRepo.value.repo_type,
      selectedRepo.value.namespace,
      selectedRepo.value.name,
    );
  } catch (error) {
    console.error("Failed to load storage breakdown:", error);
  } finally {
    loadingStorage.value = false;
  }
}

async function loadRepoCommits() {
  if (!selectedRepo.value) return;

  loadingCommits.value = true;
  try {
    const response = await listCommits(adminStore.token, {
      repo_full_id: selectedRepo.value.full_id,
      limit: 20,
    });
    repoCommits.value = response.commits || [];
  } catch (error) {
    console.error("Failed to load commits:", error);
  } finally {
    loadingCommits.value = false;
  }
}

function formatDate(dateStr) {
  return dayjs(dateStr).format("YYYY-MM-DD HH:mm");
}

function handleSortChange({ prop, order }) {
  if (!prop || !order) {
    sortBy.value = "id";
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

function applyFilters() {
  currentPage.value = 1;
  loadRepositories();
}

function resetFilters() {
  filterRepoType.value = "";
  filterNamespace.value = "";
  currentPage.value = 1;
  loadRepositories();
}

async function handleRecalculateAll() {
  if (!checkAuth()) return;

  try {
    await ElMessageBox.confirm(
      `This will recalculate storage for ${repositories.value.length || "all"} repositories. This may take some time. Continue?`,
      "Recalculate All Repository Storage",
      {
        type: "warning",
        confirmButtonText: "Recalculate",
        cancelButtonText: "Cancel",
      },
    );

    recalculating.value = true;
    ElMessage.info("Recalculating storage for all repositories...");

    const result = await recalculateAllRepoStorage(adminStore.token, {
      repo_type: filterRepoType.value || undefined,
      namespace: filterNamespace.value || undefined,
    });

    ElMessage.success(
      `Storage recalculated: ${result.success_count}/${result.total} succeeded${result.failure_count > 0 ? `, ${result.failure_count} failed` : ""}`,
    );

    // Reload repositories to show updated storage
    await loadRepositories();
  } catch (err) {
    if (err !== "cancel") {
      console.error("Failed to recalculate storage:", err);
      ElMessage.error(
        err.response?.data?.detail?.error ||
          "Failed to recalculate repository storage",
      );
    }
  } finally {
    recalculating.value = false;
  }
}

function getProgressColor(percentage) {
  if (percentage >= 90) return "danger";
  if (percentage >= 75) return "warning";
  return "success";
}

// Repository actions
async function confirmMoveRepo() {
  if (!moveForm.value.toNamespace || !moveForm.value.toName) {
    ElMessage.warning("Please enter both namespace and name");
    return;
  }

  const from = selectedRepo.value.full_id;
  const to = `${moveForm.value.toNamespace}/${moveForm.value.toName}`;

  try {
    await ElMessageBox.confirm(
      `Move repository from ${from} to ${to}?`,
      "Confirm Move",
      { type: "warning" },
    );

    actionLoading.value = true;
    await moveRepositoryAdmin(
      adminStore.token,
      selectedRepo.value.repo_type,
      selectedRepo.value.namespace,
      selectedRepo.value.name,
      moveForm.value.toNamespace,
      moveForm.value.toName,
    );
    ElMessage.success("Repository moved successfully");
    repoDialogVisible.value = false;
    loadRepositories();
  } catch (err) {
    if (err !== "cancel") {
      ElMessage.error(
        err.response?.data?.detail || "Failed to move repository",
      );
    }
  } finally {
    actionLoading.value = false;
  }
}

async function confirmSquashRepo() {
  if (!squashEnabled.value) return;
  try {
    const { value } = await ElMessageBox.prompt(
      `Type "${selectedRepo.value.name}" to confirm squash`,
      "Confirm Squash",
      {
        inputPattern: new RegExp(`^${selectedRepo.value.name}$`),
        inputErrorMessage: "Name does not match",
      },
    );

    actionLoading.value = true;
    await squashRepositoryAdmin(
      adminStore.token,
      selectedRepo.value.repo_type,
      selectedRepo.value.namespace,
      selectedRepo.value.name,
    );
    ElMessage.success("Repository squashed successfully");
    await handleViewRepo(selectedRepo.value);
  } catch (err) {
    if (err !== "cancel") {
      const detail = err.response?.data?.detail;
      ElMessage.error(
        typeof detail === "string"
          ? detail
          : detail?.error || "Failed to squash",
      );
    }
  } finally {
    actionLoading.value = false;
  }
}

async function confirmDeleteRepo() {
  try {
    const { value } = await ElMessageBox.prompt(
      'Type "DELETE" to confirm permanent deletion',
      "Confirm Delete",
      { inputPattern: /^DELETE$/, inputErrorMessage: "Must type DELETE" },
    );

    actionLoading.value = true;
    await deleteRepositoryAdmin(
      adminStore.token,
      selectedRepo.value.repo_type,
      selectedRepo.value.namespace,
      selectedRepo.value.name,
    );
    ElMessage.success("Repository deleted successfully");
    repoDialogVisible.value = false;
    loadRepositories();
  } catch (err) {
    if (err !== "cancel") {
      ElMessage.error(err.response?.data?.detail || "Failed to delete");
    }
  } finally {
    actionLoading.value = false;
  }
}

onMounted(() => {
  loadRepositories();
  loadOperationCapabilities();
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-3xl font-bold text-gray-900 dark:text-gray-100">
          Repository Management
        </h1>
        <el-button
          type="warning"
          @click="handleRecalculateAll"
          :loading="recalculating"
        >
          <span class="mr-2">🔄</span>
          Recalculate All Storage
        </el-button>
      </div>

      <!-- Search Bar -->
      <el-card class="mb-4">
        <div class="flex gap-4 items-center">
          <el-input
            v-model="searchQuery"
            placeholder="Search repositories by name or full ID..."
            clearable
            @input="handleSearchInput"
            @clear="clearSearch"
            style="max-width: 500px"
          >
            <template #prefix>
              <div class="i-carbon-search text-gray-400" />
            </template>
          </el-input>
          <span v-if="searchQuery" class="text-sm text-gray-500">
            Searching for: "{{ searchQuery }}"
          </span>
        </div>
      </el-card>

      <!-- Filters -->
      <el-card class="mb-4">
        <div class="flex gap-4 items-end">
          <el-form-item label="Repository Type" class="mb-0">
            <el-select
              v-model="filterRepoType"
              placeholder="All Types"
              clearable
              style="width: 150px"
            >
              <el-option label="Model" value="model" />
              <el-option label="Dataset" value="dataset" />
              <el-option label="Space" value="space" />
            </el-select>
          </el-form-item>

          <el-form-item label="Namespace" class="mb-0">
            <el-input
              v-model="filterNamespace"
              placeholder="Filter by namespace"
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

      <!-- Repositories Table -->
      <el-card>
        <el-empty
          v-if="!loading && repositories.length === 0"
          description="No repositories found"
        />
        <el-table
          v-else
          :data="sortedRepos"
          v-loading="loading"
          stripe
          @sort-change="handleSortChange"
          :default-sort="{ prop: 'id', order: 'descending' }"
        >
          <el-table-column prop="id" label="ID" width="80" sortable="custom" />
          <el-table-column label="Type" width="100">
            <template #default="{ row }">
              <el-tag :type="getRepoTypeColor(row.repo_type)" size="small">
                {{ row.repo_type }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column
            prop="full_id"
            label="Repository"
            min-width="250"
            sortable="custom"
          >
            <template #default="{ row }">
              <div class="flex items-center gap-2">
                <span class="font-mono">{{ row.full_id }}</span>
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
          <el-table-column
            prop="owner_username"
            label="Owner"
            width="150"
            sortable="custom"
          />
          <el-table-column
            label="Storage Used"
            width="140"
            sortable="custom"
            prop="used_bytes"
          >
            <template #default="{ row }">
              <span class="font-mono">{{ formatBytes(row.used_bytes) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Quota" width="120">
            <template #default="{ row }">
              <span v-if="row.quota_bytes" class="font-mono text-sm">
                {{ formatBytes(row.quota_bytes) }}
              </span>
              <span v-else class="text-gray-400 text-sm">Inherit</span>
            </template>
          </el-table-column>
          <el-table-column label="% Used" width="140">
            <template #default="{ row }">
              <div
                v-if="
                  row.percentage_used !== null &&
                  row.percentage_used !== undefined
                "
              >
                <el-progress
                  :percentage="Math.min(100, row.percentage_used)"
                  :status="getProgressColor(row.percentage_used)"
                  :format="(p) => `${p.toFixed(1)}%`"
                />
              </div>
              <span v-else class="text-gray-400">-</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="created_at"
            label="Created"
            width="160"
            sortable="custom"
          >
            <template #default="{ row }">
              {{ formatDate(row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column label="Actions" width="120" fixed="right">
            <template #default="{ row }">
              <el-button size="small" @click="handleViewRepo(row)">
                View Details
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <!-- Pagination -->
        <div class="mt-4 flex justify-center">
          <el-pagination
            v-model:current-page="currentPage"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            :total="totalCount"
            @current-change="loadRepositories"
            @size-change="loadRepositories"
          />
        </div>
      </el-card>

      <!-- Repository Details Dialog with Tabs -->
      <el-dialog
        v-model="repoDialogVisible"
        width="900px"
        :title="selectedRepo ? selectedRepo.full_id : 'Repository Details'"
      >
        <div v-if="selectedRepo" class="repo-details">
          <el-tabs v-model="activeTab">
            <!-- Overview Tab -->
            <el-tab-pane label="Overview" name="overview">
              <div class="mb-4">
                <el-tag
                  :type="getRepoTypeColor(selectedRepo.repo_type)"
                  class="mr-2"
                >
                  {{ selectedRepo.repo_type }}
                </el-tag>
                <el-tag
                  :type="selectedRepo.private ? 'warning' : 'success'"
                  effect="plain"
                >
                  {{ selectedRepo.private ? "Private" : "Public" }}
                </el-tag>
              </div>

              <el-descriptions :column="2" border>
                <el-descriptions-item label="ID">{{
                  selectedRepo.id
                }}</el-descriptions-item>
                <el-descriptions-item label="Owner">{{
                  selectedRepo.owner_username
                }}</el-descriptions-item>
                <el-descriptions-item label="Namespace">{{
                  selectedRepo.namespace
                }}</el-descriptions-item>
                <el-descriptions-item label="Name">{{
                  selectedRepo.name
                }}</el-descriptions-item>
                <el-descriptions-item label="LakeFS repository" :span="2">
                  <span class="font-mono" data-testid="repo-lakefs-id">{{
                    selectedRepo.lakefs_repo
                  }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="Created">{{
                  formatDate(selectedRepo.created_at)
                }}</el-descriptions-item>
                <el-descriptions-item label="Files">
                  <strong>{{ selectedRepo.file_count }}</strong>
                </el-descriptions-item>
                <el-descriptions-item label="Commits">
                  <strong>{{ selectedRepo.commit_count }}</strong>
                </el-descriptions-item>
                <el-descriptions-item label="Total Size">
                  <strong>{{ formatBytes(selectedRepo.total_size) }}</strong>
                </el-descriptions-item>
                <el-descriptions-item label="Storage Used">
                  <strong>{{ formatBytes(selectedRepo.used_bytes) }}</strong>
                </el-descriptions-item>
                <el-descriptions-item label="Repository Quota">
                  <span v-if="selectedRepo.quota_bytes">
                    <strong>{{ formatBytes(selectedRepo.quota_bytes) }}</strong>
                  </span>
                  <span v-else class="text-gray-400"
                    >Inherit from namespace</span
                  >
                </el-descriptions-item>
              </el-descriptions>

              <div class="mt-4">
                <el-button
                  type="primary"
                  @click="
                    $router.push(
                      `/${selectedRepo.repo_type}s/${selectedRepo.namespace}/${selectedRepo.name}`,
                    )
                  "
                >
                  <div class="i-carbon-launch mr-2" />
                  View in Main App
                </el-button>
              </div>
            </el-tab-pane>

            <!-- Files Tab -->
            <el-tab-pane label="Files" name="files">
              <FileTree
                :repo-type="selectedRepo.repo_type"
                :namespace="selectedRepo.namespace"
                :name="selectedRepo.name"
                :token="adminStore.token"
              />
            </el-tab-pane>

            <!-- Commits Tab -->
            <el-tab-pane label="Commits" name="commits">
              <div
                v-if="
                  activeTab === 'commits' &&
                  !loadingCommits &&
                  repoCommits.length === 0
                "
              >
                <el-button
                  type="primary"
                  size="small"
                  @click="loadRepoCommits"
                  class="mb-4"
                >
                  Load Commits
                </el-button>
              </div>

              <el-table
                v-if="repoCommits.length > 0"
                :data="repoCommits"
                v-loading="loadingCommits"
                stripe
                max-height="400"
              >
                <el-table-column label="SHA" width="100">
                  <template #default="{ row }">
                    <code class="text-xs">{{
                      row.commit_id.substring(0, 8)
                    }}</code>
                  </template>
                </el-table-column>
                <el-table-column prop="username" label="Author" width="120" />
                <el-table-column label="Message" min-width="250">
                  <template #default="{ row }">
                    <div class="text-sm">{{ row.message }}</div>
                  </template>
                </el-table-column>
                <el-table-column prop="branch" label="Branch" width="100" />
                <el-table-column label="Date" width="160">
                  <template #default="{ row }">
                    {{ formatDate(row.created_at) }}
                  </template>
                </el-table-column>
              </el-table>

              <div v-else-if="activeTab === 'commits'" class="text-center py-8">
                <el-button
                  type="primary"
                  @click="loadRepoCommits"
                  :loading="loadingCommits"
                >
                  Load Commits
                </el-button>
              </div>
            </el-tab-pane>

            <!-- Storage Tab -->
            <el-tab-pane label="Storage" name="storage">
              <div v-if="loadingStorage" class="text-center py-8">
                <el-skeleton :rows="5" animated />
              </div>

              <div v-else-if="storageBreakdown" class="storage-breakdown">
                <h3 class="text-lg font-semibold mb-3">Storage Breakdown</h3>

                <el-descriptions :column="2" border class="mb-4">
                  <el-descriptions-item label="Regular Files">
                    <strong>{{
                      formatBytes(storageBreakdown.regular_files_size)
                    }}</strong>
                    <span class="text-xs text-gray-500 ml-2">
                      ({{
                        (
                          (storageBreakdown.regular_files_size /
                            storageBreakdown.total_size) *
                          100
                        ).toFixed(1)
                      }}%)
                    </span>
                  </el-descriptions-item>
                  <el-descriptions-item label="LFS Files">
                    <strong>{{
                      formatBytes(storageBreakdown.lfs_files_size)
                    }}</strong>
                    <span class="text-xs text-gray-500 ml-2">
                      ({{
                        (
                          (storageBreakdown.lfs_files_size /
                            storageBreakdown.total_size) *
                          100
                        ).toFixed(1)
                      }}%)
                    </span>
                  </el-descriptions-item>
                  <el-descriptions-item label="Total Size">
                    <strong>{{
                      formatBytes(storageBreakdown.total_size)
                    }}</strong>
                  </el-descriptions-item>
                  <el-descriptions-item label="LFS Objects">
                    <strong>{{ storageBreakdown.lfs_object_count }}</strong>
                  </el-descriptions-item>
                  <el-descriptions-item label="Unique LFS Objects">
                    <strong>{{ storageBreakdown.unique_lfs_objects }}</strong>
                  </el-descriptions-item>
                  <el-descriptions-item label="Deduplication Savings">
                    <strong>{{
                      formatBytes(storageBreakdown.deduplication_savings)
                    }}</strong>
                  </el-descriptions-item>
                </el-descriptions>

                <div class="mt-4">
                  <h4 class="text-sm font-semibold mb-2">
                    Storage Distribution
                  </h4>
                  <div class="flex gap-2">
                    <div
                      class="storage-bar"
                      :style="{
                        width:
                          (storageBreakdown.regular_files_size /
                            storageBreakdown.total_size) *
                            100 +
                          '%',
                        backgroundColor: '#409EFF',
                      }"
                    >
                      <span
                        v-if="storageBreakdown.regular_files_size > 0"
                        class="text-xs text-white px-2"
                      >
                        Regular
                      </span>
                    </div>
                    <div
                      class="storage-bar"
                      :style="{
                        width:
                          (storageBreakdown.lfs_files_size /
                            storageBreakdown.total_size) *
                            100 +
                          '%',
                        backgroundColor: '#E6A23C',
                      }"
                    >
                      <span
                        v-if="storageBreakdown.lfs_files_size > 0"
                        class="text-xs text-white px-2"
                      >
                        LFS
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div v-else class="text-center py-8">
                <el-button type="primary" @click="loadStorageBreakdown">
                  Load Storage Analytics
                </el-button>
              </div>
            </el-tab-pane>

            <!-- Actions Tab -->
            <el-tab-pane label="Actions" name="actions">
              <div class="space-y-4">
                <!-- Move Repository -->
                <el-card class="bg-white dark:bg-gray-800">
                  <template #header>
                    <div class="font-semibold">Move/Rename Repository</div>
                  </template>
                  <el-form label-position="top">
                    <el-form-item label="To Namespace">
                      <el-input
                        v-model="moveForm.toNamespace"
                        placeholder="target-org"
                      />
                    </el-form-item>
                    <el-form-item label="To Name">
                      <el-input
                        v-model="moveForm.toName"
                        placeholder="new-name"
                      />
                    </el-form-item>
                    <el-button
                      type="primary"
                      @click="confirmMoveRepo"
                      :loading="actionLoading"
                    >
                      Move Repository
                    </el-button>
                  </el-form>
                </el-card>

                <!-- Squash Repository -->
                <el-card v-if="squashEnabled" class="bg-white dark:bg-gray-800">
                  <template #header>
                    <div class="font-semibold">Squash Repository</div>
                  </template>
                  <el-alert type="warning" :closable="false" class="mb-4">
                    Clears all commit history. Only current state preserved.
                    IRREVERSIBLE!
                  </el-alert>
                  <el-button
                    type="warning"
                    @click="confirmSquashRepo"
                    :loading="actionLoading"
                  >
                    Squash Repository
                  </el-button>
                </el-card>

                <!-- Delete Repository -->
                <el-card class="bg-white dark:bg-gray-800">
                  <template #header>
                    <div class="font-semibold text-red-600">
                      Delete Repository
                    </div>
                  </template>
                  <el-alert type="error" :closable="false" class="mb-4">
                    IRREVERSIBLE! Deletes all files, history, and metadata.
                  </el-alert>
                  <el-button
                    type="danger"
                    @click="confirmDeleteRepo"
                    :loading="actionLoading"
                  >
                    Delete Repository
                  </el-button>
                </el-card>
              </div>
            </el-tab-pane>
          </el-tabs>
        </div>

        <template #footer>
          <el-button @click="repoDialogVisible = false">Close</el-button>
        </template>
      </el-dialog>
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.repo-details {
  padding: 12px 0;
}

.storage-breakdown {
  padding: 12px 0;
}

.storage-bar {
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  font-weight: 600;
  font-size: 13px;
  letter-spacing: 0.5px;
  transition: all 0.3s ease;
  box-shadow: var(--shadow-sm);
}

.storage-bar:hover {
  opacity: 0.85;
  transform: scale(1.02);
}

/* Card styling */
:deep(.el-card) {
  background-color: var(--bg-card);
  border-color: var(--border-default);
  transition: all 0.2s ease;
}

:deep(.el-card:hover) {
  box-shadow: var(--shadow-md);
}

/* Table styling */
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

/* Tabs styling */
:deep(.el-tabs__item) {
  color: var(--text-secondary);
  font-weight: 500;
}

:deep(.el-tabs__item.is-active) {
  color: var(--color-info);
  font-weight: 600;
}

:deep(.el-tabs__item:hover) {
  color: var(--color-info);
}

:deep(.el-descriptions__label) {
  color: var(--text-secondary);
  font-weight: 600;
}

:deep(.el-descriptions__content) {
  color: var(--text-primary);
}
</style>
