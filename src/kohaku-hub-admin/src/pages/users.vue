<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import { useAdminStore } from "@/stores/admin";
import {
  listUsers,
  getUserInfo,
  createUser,
  deleteUser,
  setEmailVerification,
  updateUserQuota,
} from "@/utils/api";
import { formatBytes } from "@/utils/api";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();
const users = ref([]);
const loading = ref(false);
const dialogVisible = ref(false);
const userDialogVisible = ref(false);
const quotaDialogVisible = ref(false);
const selectedUser = ref(null);
const quotaForm = ref({
  username: "",
  private_quota_bytes: null,
  public_quota_bytes: null,
});

const quotaInputPrivate = ref("");
const quotaInputPublic = ref("");

// Parse human-readable size (100G, 5TB, 500MB) to bytes (decimal 1000^k)
function parseHumanSize(input) {
  if (!input || input === "null" || input === "unlimited") return null;

  const match = input.trim().match(/^(\d+(?:\.\d+)?)\s*([KMGTB]+)?$/i);
  if (!match) return null;

  const value = parseFloat(match[1]);
  const unit = (match[2] || "").toUpperCase().replace("B", "");

  const multipliers = {
    "": 1,
    K: 1000,
    M: 1000000,
    G: 1000000000,
    T: 1000000000000,
  };

  return Math.floor(value * (multipliers[unit] || 1));
}

// Format bytes to human-readable (decimal)
function formatBytesDecimal(bytes) {
  if (bytes === null || bytes === undefined) return "Unlimited";
  if (bytes === 0) return "0 B";

  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;

  while (size >= 1000 && unitIndex < units.length - 1) {
    size /= 1000;
    unitIndex++;
  }

  return `${size.toFixed(2)} ${units[unitIndex]}`;
}

// Search and filters
const searchQuery = ref("");
const searchDebounceTimer = ref(null);
const showOrgs = ref(true); // Toggle to show/hide organizations

// Pagination
const currentPage = ref(1);
const pageSize = ref(20);

// Sorting
const sortBy = ref("id");
const sortOrder = ref("ascending");

// Computed sorted users
const sortedUsers = computed(() => {
  if (!users.value || users.value.length === 0) return [];

  const sorted = [...users.value];
  sorted.sort((a, b) => {
    let aVal, bVal;

    switch (sortBy.value) {
      case "id":
        aVal = a.id;
        bVal = b.id;
        break;
      case "username":
        aVal = a.username?.toLowerCase() || "";
        bVal = b.username?.toLowerCase() || "";
        break;
      case "private_used":
        aVal = a.private_used_bytes || 0;
        bVal = b.private_used_bytes || 0;
        break;
      case "public_used":
        aVal = a.public_used_bytes || 0;
        bVal = b.public_used_bytes || 0;
        break;
      case "total_used":
        aVal = (a.private_used_bytes || 0) + (a.public_used_bytes || 0);
        bVal = (b.private_used_bytes || 0) + (b.public_used_bytes || 0);
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

// Create user form
const createForm = ref({
  username: "",
  email: "",
  password: "",
  email_verified: false,
  private_quota_bytes: null,
  public_quota_bytes: null,
});

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadUsers() {
  if (!checkAuth()) return;

  loading.value = true;
  try {
    const response = await listUsers(adminStore.token, {
      search: searchQuery.value || undefined,
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
      include_orgs: showOrgs.value,
    });
    users.value = response.users;
  } catch (error) {
    console.error("Failed to load users:", error);
    if (error.response?.status === 401 || error.response?.status === 403) {
      ElMessage.error("Invalid admin token. Please login again.");
      adminStore.logout();
      router.push("/login");
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to load users",
      );
    }
  } finally {
    loading.value = false;
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
    loadUsers();
  }, 500);
}

function clearSearch() {
  searchQuery.value = "";
  loadUsers();
}

async function handleViewUser(row) {
  if (!checkAuth()) return;

  try {
    selectedUser.value = await getUserInfo(adminStore.token, row.username);
    userDialogVisible.value = true;
  } catch (error) {
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to load user info",
    );
  }
}

async function handleCreateUser() {
  if (!checkAuth()) return;

  try {
    await createUser(adminStore.token, createForm.value);
    ElMessage.success("User created successfully");
    dialogVisible.value = false;
    resetForm();
    loadUsers();
  } catch (error) {
    console.error("Failed to create user:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to create user",
    );
  }
}

async function handleDeleteUser(row) {
  if (!checkAuth()) return;

  try {
    await ElMessageBox.confirm(
      `Are you sure you want to delete user "${row.username}"?

This will delete:
- All sessions and tokens
- All organization memberships

Repositories owned by this user will NOT be deleted unless you use force delete.`,
      "Confirm Delete",
      {
        confirmButtonText: "Delete",
        cancelButtonText: "Cancel",
        type: "warning",
        distinguishCancelAndClose: true,
        confirmButtonClass: "el-button--danger",
      },
    );

    await deleteUser(adminStore.token, row.username, false);
    ElMessage.success("User deleted successfully");
    loadUsers();
  } catch (error) {
    if (error === "cancel" || error === "close") return;

    // Check if user owns repositories
    if (error.response?.data?.detail?.owned_repositories) {
      const repos = error.response.data.detail.owned_repositories;
      try {
        await ElMessageBox.confirm(
          `User owns ${repos.length} repository(ies):

${repos.join("\n")}

Do you want to FORCE DELETE the user and ALL their repositories?
Their LakeFS repositories, S3 data and unshared LFS objects are removed by
background tasks. This action CANNOT be undone!`,
          "Force Delete Required",
          {
            confirmButtonText: "Force Delete Everything",
            cancelButtonText: "Cancel",
            type: "error",
            confirmButtonClass: "el-button--danger",
          },
        );

        await deleteUser(adminStore.token, row.username, true);
        ElMessage.success(
          "User and repositories deleted; storage cleanup scheduled (see Background Tasks)",
        );
        loadUsers();
      } catch (forceError) {
        if (forceError !== "cancel" && forceError !== "close") {
          ElMessage.error("Failed to delete user");
        }
      }
    } else {
      ElMessage.error(
        error.response?.data?.detail?.error || "Failed to delete user",
      );
    }
  }
}

function openQuotaDialog() {
  quotaForm.value = {
    username: selectedUser.value.username,
    private_quota_bytes: selectedUser.value.private_quota_bytes,
    public_quota_bytes: selectedUser.value.public_quota_bytes,
  };

  // Set human-readable inputs
  quotaInputPrivate.value =
    selectedUser.value.private_quota_bytes !== null
      ? formatBytesDecimal(selectedUser.value.private_quota_bytes)
      : "unlimited";
  quotaInputPublic.value =
    selectedUser.value.public_quota_bytes !== null
      ? formatBytesDecimal(selectedUser.value.public_quota_bytes)
      : "unlimited";

  quotaDialogVisible.value = true;
}

function updateQuotaFromInput(type) {
  const input =
    type === "private" ? quotaInputPrivate.value : quotaInputPublic.value;
  const bytes = parseHumanSize(input);

  if (type === "private") {
    quotaForm.value.private_quota_bytes = bytes;
  } else {
    quotaForm.value.public_quota_bytes = bytes;
  }
}

async function saveQuota() {
  if (!checkAuth()) return;

  try {
    loading.value = true;
    await updateUserQuota(
      adminStore.token,
      quotaForm.value.username,
      quotaForm.value.private_quota_bytes,
      quotaForm.value.public_quota_bytes,
    );

    ElMessage.success("Quota updated successfully");
    quotaDialogVisible.value = false;
    userDialogVisible.value = false;
    await loadUsers();
  } catch (error) {
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to update quota",
    );
  } finally {
    loading.value = false;
  }
}

async function handleToggleEmailVerification(row) {
  if (!checkAuth()) return;

  const newStatus = !row.email_verified;
  try {
    await setEmailVerification(adminStore.token, row.username, newStatus);
    ElMessage.success(
      `Email ${newStatus ? "verified" : "unverified"} successfully`,
    );
    loadUsers();
  } catch (error) {
    ElMessage.error(
      error.response?.data?.detail?.error ||
        "Failed to update email verification",
    );
  }
}

function resetForm() {
  createForm.value = {
    username: "",
    email: "",
    password: "",
    email_verified: false,
    private_quota_bytes: null,
    public_quota_bytes: null,
  };
}

function formatDate(dateStr) {
  return dayjs(dateStr).format("YYYY-MM-DD HH:mm");
}

function handleSortChange({ prop, order }) {
  if (!prop || !order) {
    sortBy.value = "id";
    sortOrder.value = "ascending";
    return;
  }
  sortBy.value = prop;
  sortOrder.value = order;
}

function isQuotaExceeded(used, quota) {
  if (quota === null || quota === undefined) return false; // Unlimited
  return used > quota;
}

onMounted(() => {
  loadUsers();
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-3xl font-bold text-gray-900 dark:text-gray-100">
          User Management
        </h1>
        <el-button type="primary" @click="dialogVisible = true" :icon="'Plus'">
          Create User
        </el-button>
      </div>

      <!-- Search Bar -->
      <el-card class="mb-4">
        <div class="flex gap-4 items-center flex-wrap">
          <el-input
            v-model="searchQuery"
            placeholder="Search users by username or email..."
            clearable
            @input="handleSearchInput"
            @clear="clearSearch"
            style="max-width: 500px"
          >
            <template #prefix>
              <div class="i-carbon-search text-gray-400" />
            </template>
          </el-input>
          <el-switch
            v-model="showOrgs"
            @change="loadUsers"
            active-text="Show Organizations"
            inactive-text="Users Only"
          />
          <span v-if="searchQuery" class="text-sm text-gray-500">
            Searching for: "{{ searchQuery }}"
          </span>
        </div>
      </el-card>

      <!-- Users Table -->
      <el-card>
        <el-empty
          v-if="!loading && users.length === 0"
          description="No users found"
        />
        <el-table
          v-else
          :data="sortedUsers"
          v-loading="loading"
          stripe
          @sort-change="handleSortChange"
          :default-sort="{ prop: 'id', order: 'ascending' }"
        >
          <el-table-column prop="id" label="ID" width="80" sortable="custom" />
          <el-table-column
            prop="username"
            label="Username"
            min-width="150"
            sortable="custom"
          >
            <template #default="{ row }">
              <div class="flex items-center gap-2">
                <span>{{ row.username }}</span>
                <el-tag v-if="row.is_org" size="small" type="warning"
                  >ORG</el-tag
                >
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="email" label="Email" min-width="200" />
          <el-table-column
            prop="private_used"
            label="Private Storage"
            width="140"
            align="right"
            sortable="custom"
          >
            <template #default="{ row }">
              <div class="storage-cell">
                <span
                  :class="{
                    'text-danger': isQuotaExceeded(
                      row.private_used_bytes,
                      row.private_quota_bytes,
                    ),
                  }"
                >
                  {{ formatBytes(row.private_used_bytes) }}
                </span>
                <span class="text-gray-400 text-xs">
                  / {{ formatBytes(row.private_quota_bytes) }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="public_used"
            label="Public Storage"
            width="140"
            align="right"
            sortable="custom"
          >
            <template #default="{ row }">
              <div class="storage-cell">
                <span
                  :class="{
                    'text-danger': isQuotaExceeded(
                      row.public_used_bytes,
                      row.public_quota_bytes,
                    ),
                  }"
                >
                  {{ formatBytes(row.public_used_bytes) }}
                </span>
                <span class="text-gray-400 text-xs">
                  / {{ formatBytes(row.public_quota_bytes) }}
                </span>
              </div>
            </template>
          </el-table-column>
          <el-table-column
            prop="total_used"
            label="Total Storage"
            width="140"
            align="right"
            sortable="custom"
          >
            <template #default="{ row }">
              {{
                formatBytes(
                  (row.private_used_bytes || 0) + (row.public_used_bytes || 0),
                )
              }}
            </template>
          </el-table-column>
          <el-table-column label="Email Verified" width="130" align="center">
            <template #default="{ row }">
              <el-tag
                :type="row.email_verified ? 'success' : 'warning'"
                size="small"
              >
                {{ row.email_verified ? "Verified" : "Unverified" }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Active" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
                {{ row.is_active ? "Active" : "Inactive" }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="Created" width="160">
            <template #default="{ row }">
              {{ formatDate(row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column label="Actions" width="300" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                @click="handleViewUser(row)"
                :icon="'View'"
              >
                View
              </el-button>
              <el-button
                size="small"
                :type="row.email_verified ? 'warning' : 'success'"
                @click="handleToggleEmailVerification(row)"
              >
                {{ row.email_verified ? "Unverify" : "Verify" }}
              </el-button>
              <el-button
                size="small"
                type="danger"
                @click="handleDeleteUser(row)"
                :icon="'Delete'"
              >
                Delete
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- Create User Dialog -->
      <el-dialog
        v-model="dialogVisible"
        title="Create New User"
        width="600px"
        @close="resetForm"
      >
        <el-form :model="createForm" label-width="180px">
          <el-form-item label="Username" required>
            <el-input
              v-model="createForm.username"
              placeholder="Enter username"
            />
          </el-form-item>

          <el-form-item label="Email" required>
            <el-input
              v-model="createForm.email"
              type="email"
              placeholder="Enter email"
            />
          </el-form-item>

          <el-form-item label="Password" required>
            <el-input
              v-model="createForm.password"
              type="password"
              placeholder="Enter password"
              show-password
            />
          </el-form-item>

          <el-form-item label="Email Verified">
            <el-switch v-model="createForm.email_verified" />
          </el-form-item>

          <el-form-item label="Private Quota">
            <el-input
              v-model="createForm.private_quota_bytes"
              type="number"
              placeholder="Leave empty for unlimited"
            >
              <template #append>bytes</template>
            </el-input>
            <div class="text-xs text-gray-500 mt-1">
              Example: 10GB = 10737418240 bytes (leave empty for unlimited)
            </div>
          </el-form-item>

          <el-form-item label="Public Quota">
            <el-input
              v-model="createForm.public_quota_bytes"
              type="number"
              placeholder="Leave empty for unlimited"
            >
              <template #append>bytes</template>
            </el-input>
            <div class="text-xs text-gray-500 mt-1">
              Example: 50GB = 53687091200 bytes (leave empty for unlimited)
            </div>
          </el-form-item>
        </el-form>

        <template #footer>
          <el-button @click="dialogVisible = false">Cancel</el-button>
          <el-button type="primary" @click="handleCreateUser"
            >Create User</el-button
          >
        </template>
      </el-dialog>

      <!-- User Details Dialog -->
      <el-dialog v-model="userDialogVisible" title="User Details" width="700px">
        <div v-if="selectedUser" class="user-details">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="ID">{{
              selectedUser.id
            }}</el-descriptions-item>
            <el-descriptions-item label="Username">{{
              selectedUser.username
            }}</el-descriptions-item>
            <el-descriptions-item label="Email">{{
              selectedUser.email
            }}</el-descriptions-item>
            <el-descriptions-item label="Email Verified">
              <el-tag
                :type="selectedUser.email_verified ? 'success' : 'warning'"
              >
                {{ selectedUser.email_verified ? "Yes" : "No" }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="Active">
              <el-tag :type="selectedUser.is_active ? 'success' : 'danger'">
                {{ selectedUser.is_active ? "Yes" : "No" }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="Created">{{
              formatDate(selectedUser.created_at)
            }}</el-descriptions-item>
          </el-descriptions>

          <el-divider />

          <h3 class="text-lg font-bold mb-3">Storage Quotas</h3>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="Private Quota">
              {{ formatBytes(selectedUser.private_quota_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Private Used">
              {{ formatBytes(selectedUser.private_used_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Public Quota">
              {{ formatBytes(selectedUser.public_quota_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Public Used">
              {{ formatBytes(selectedUser.public_used_bytes) }}
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <template #footer>
          <el-button @click="userDialogVisible = false">Close</el-button>
          <el-button type="primary" @click="openQuotaDialog">
            Edit Quota
          </el-button>
        </template>
      </el-dialog>

      <!-- Quota Edit Dialog -->
      <el-dialog
        v-model="quotaDialogVisible"
        title="Edit Storage Quota"
        width="600px"
      >
        <el-form :model="quotaForm" label-width="150px">
          <el-form-item label="Username">
            <span>{{ quotaForm.username }}</span>
          </el-form-item>

          <el-divider content-position="left">Public Quota</el-divider>

          <el-form-item label="Public Quota">
            <el-input
              v-model="quotaInputPublic"
              @blur="updateQuotaFromInput('public')"
              placeholder="e.g., 10G, 500MB, unlimited"
            >
              <template #append>
                <span class="text-xs"
                  >=
                  {{ formatBytesDecimal(quotaForm.public_quota_bytes) }}</span
                >
              </template>
            </el-input>
            <div class="text-xs text-gray-500 mt-1">
              Enter: 100G, 5TB, 500MB, or "unlimited" (decimal: 1GB =
              1,000,000,000 bytes)
            </div>
          </el-form-item>

          <el-form-item label="Quick Presets">
            <div class="flex gap-2 flex-wrap">
              <el-button
                size="small"
                @click="
                  quotaInputPublic = '5G';
                  updateQuotaFromInput('public');
                "
                >5GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPublic = '10G';
                  updateQuotaFromInput('public');
                "
                >10GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPublic = '50G';
                  updateQuotaFromInput('public');
                "
                >50GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPublic = '100G';
                  updateQuotaFromInput('public');
                "
                >100GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPublic = 'unlimited';
                  updateQuotaFromInput('public');
                "
                >Unlimited</el-button
              >
            </div>
          </el-form-item>

          <el-divider content-position="left">Private Quota</el-divider>

          <el-form-item label="Private Quota">
            <el-input
              v-model="quotaInputPrivate"
              @blur="updateQuotaFromInput('private')"
              placeholder="e.g., 10G, 500MB, unlimited"
            >
              <template #append>
                <span class="text-xs"
                  >=
                  {{ formatBytesDecimal(quotaForm.private_quota_bytes) }}</span
                >
              </template>
            </el-input>
            <div class="text-xs text-gray-500 mt-1">
              Enter: 100G, 5TB, 500MB, or "unlimited"
            </div>
          </el-form-item>

          <el-form-item label="Quick Presets">
            <div class="flex gap-2 flex-wrap">
              <el-button
                size="small"
                @click="
                  quotaInputPrivate = '5G';
                  updateQuotaFromInput('private');
                "
                >5GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPrivate = '10G';
                  updateQuotaFromInput('private');
                "
                >10GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPrivate = '50G';
                  updateQuotaFromInput('private');
                "
                >50GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPrivate = '100G';
                  updateQuotaFromInput('private');
                "
                >100GB</el-button
              >
              <el-button
                size="small"
                @click="
                  quotaInputPrivate = 'unlimited';
                  updateQuotaFromInput('private');
                "
                >Unlimited</el-button
              >
            </div>
          </el-form-item>
        </el-form>

        <template #footer>
          <el-button @click="quotaDialogVisible = false">Cancel</el-button>
          <el-button type="primary" @click="saveQuota" :loading="loading">
            Save
          </el-button>
        </template>
      </el-dialog>
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.user-details {
  padding: 12px 0;
}

.storage-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.5;
  gap: 2px;
}

.storage-cell span:first-child {
  font-weight: 600;
  font-family: "SF Mono", "Monaco", "Consolas", monospace;
}

.text-danger {
  color: var(--color-error) !important;
  font-weight: 700;
}

.text-gray-400 {
  color: var(--text-tertiary);
  font-size: 12px;
}

/* Search bar styling */
:deep(.el-card) {
  background-color: var(--bg-card);
  border-color: var(--border-default);
}

:deep(.el-input__wrapper) {
  background-color: var(--bg-hover);
  border-color: var(--border-default);
  transition: all 0.2s ease;
}

:deep(.el-input__wrapper:hover) {
  border-color: var(--color-info);
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
