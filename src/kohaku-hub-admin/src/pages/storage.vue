<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import OrphanLakefsRepos from "@/components/storage/OrphanLakefsRepos.vue";
import { useAdminStore } from "@/stores/admin";
import {
  listS3Buckets,
  listS3Objects,
  deleteS3Object,
  prepareDeleteS3Prefix,
  deleteS3Prefix,
} from "@/utils/api";
import { formatBytes } from "@/utils/api";
import { ElMessage, ElMessageBox } from "element-plus";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();
const objects = ref([]);
const loading = ref(false);
const currentPath = ref("");
const pathParts = ref([]);

// Computed: Parse objects into folders and files
const folderStructure = computed(() => {
  if (!objects.value || objects.value.length === 0) {
    return { folders: [], files: [] };
  }

  const folders = new Set();
  const files = [];
  const prefix = currentPath.value;

  objects.value.forEach((obj) => {
    const key = obj.key;

    // Skip if doesn't start with current path
    if (prefix && !key.startsWith(prefix)) return;

    // Get relative path from current prefix
    const relativePath = prefix ? key.substring(prefix.length) : key;

    // Check if this is a folder or file in current directory
    const slashIndex = relativePath.indexOf("/");

    if (slashIndex === -1) {
      // It's a file in current directory
      files.push({ ...obj, name: relativePath });
    } else {
      // It's inside a subfolder
      const folderName = relativePath.substring(0, slashIndex);
      folders.add(folderName);
    }
  });

  return {
    folders: Array.from(folders).sort(),
    files: files.sort((a, b) => a.name.localeCompare(b.name)),
  };
});

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadObjects(prefix = "") {
  if (!checkAuth()) return;

  loading.value = true;
  currentPath.value = prefix;

  // Update breadcrumb path parts
  if (prefix) {
    pathParts.value = prefix.split("/").filter((p) => p);
  } else {
    pathParts.value = [];
  }

  try {
    // Just list objects from the default configured bucket
    const response = await listS3Objects(adminStore.token, "", {
      prefix,
      limit: 1000,
    });
    objects.value = response.objects;
  } catch (error) {
    console.error("Failed to load objects:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to load storage objects",
    );
  } finally {
    loading.value = false;
  }
}

function navigateToFolder(folderName) {
  const newPath = currentPath.value + folderName + "/";
  loadObjects(newPath);
}

function navigateToBreadcrumb(index) {
  if (index === -1) {
    // Navigate to root
    loadObjects("");
  } else {
    // Navigate to specific path level
    const newPath = pathParts.value.slice(0, index + 1).join("/") + "/";
    loadObjects(newPath);
  }
}

function navigateUp() {
  if (pathParts.value.length === 0) {
    // Already at root
    return;
  } else {
    // Go up one level
    navigateToBreadcrumb(pathParts.value.length - 2);
  }
}

function formatDate(dateStr) {
  return dayjs(dateStr).format("YYYY-MM-DD HH:mm:ss");
}

function getFileIcon(fileName) {
  const ext = fileName.split(".").pop().toLowerCase();
  const iconMap = {
    // Archives
    zip: "i-carbon-archive",
    tar: "i-carbon-archive",
    gz: "i-carbon-archive",
    // Images
    jpg: "i-carbon-image",
    jpeg: "i-carbon-image",
    png: "i-carbon-image",
    gif: "i-carbon-image",
    // Documents
    pdf: "i-carbon-document-pdf",
    txt: "i-carbon-document",
    md: "i-carbon-document",
    // Code
    py: "i-carbon-logo-python",
    js: "i-carbon-code",
    json: "i-carbon-code",
    // Default
    default: "i-carbon-document",
  };
  return iconMap[ext] || iconMap.default;
}

async function confirmDeleteObject(key) {
  try {
    await ElMessageBox.confirm(`Delete object: ${key}?`, "Confirm Delete", {
      type: "warning",
    });

    await deleteS3Object(adminStore.token, key);
    ElMessage.success("Object deleted");
    loadObjects(currentPath.value); // Refresh
  } catch (err) {
    if (err !== "cancel") {
      ElMessage.error("Failed to delete object");
    }
  }
}

async function confirmDeleteFolder(folderName) {
  const prefix = currentPath.value + folderName + "/";

  // Step 1: Prepare deletion (get count)
  let prepareResult;
  try {
    prepareResult = await prepareDeleteS3Prefix(adminStore.token, prefix);
  } catch (err) {
    ElMessage.error("Failed to prepare deletion");
    return;
  }

  // Step 2: Confirm with count
  try {
    await ElMessageBox.confirm(
      `Delete ${prepareResult.estimated_objects} objects under ${prefix}? This is IRREVERSIBLE!`,
      "Confirm Folder Deletion",
      { type: "error", confirmButtonText: "Delete All" },
    );

    // Step 3: Execute deletion
    const result = await deleteS3Prefix(
      adminStore.token,
      prefix,
      prepareResult.confirm_token,
    );
    ElMessage.success(`Deleted ${result.deleted_count} objects`);
    loadObjects(currentPath.value); // Refresh
  } catch (err) {
    if (err !== "cancel") {
      ElMessage.error(err.response?.data?.detail || "Failed to delete folder");
    }
  }
}

onMounted(() => {
  loadObjects(""); // Load root directly
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-3xl font-bold text-gray-900 dark:text-gray-100">
          Storage Browser
        </h1>
        <el-button
          @click="loadObjects('')"
          :icon="'Refresh'"
          :loading="loading"
        >
          Refresh
        </el-button>
      </div>

      <!-- File Explorer (Direct) -->
      <el-card>
        <template #header>
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <div class="i-carbon-folder-open text-2xl text-blue-600" />
              <span class="font-bold">Storage Explorer</span>
            </div>
            <div
              class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
            >
              <span>{{ folderStructure.folders.length }} folders</span>
              <span>•</span>
              <span>{{ folderStructure.files.length }} files</span>
              <span>•</span>
              <span>{{ objects.length }} total objects</span>
            </div>
          </div>
        </template>

        <!-- Breadcrumb Navigation -->
        <div class="breadcrumb-container">
          <el-button
            size="small"
            @click="navigateUp"
            :icon="'ArrowLeft'"
            class="mr-2"
            :disabled="pathParts.length === 0"
          >
            Up
          </el-button>

          <el-breadcrumb separator="/" class="flex-1">
            <el-breadcrumb-item
              @click="navigateToBreadcrumb(-1)"
              class="breadcrumb-item"
            >
              <div class="i-carbon-home text-blue-600" />
              <span class="ml-1">Root</span>
            </el-breadcrumb-item>
            <el-breadcrumb-item
              v-for="(part, index) in pathParts"
              :key="index"
              @click="navigateToBreadcrumb(index)"
              class="breadcrumb-item"
            >
              <div class="i-carbon-folder text-orange-600" />
              <span class="ml-1">{{ part }}</span>
            </el-breadcrumb-item>
          </el-breadcrumb>
        </div>

        <!-- File Explorer View -->
        <div v-loading="loading">
          <el-empty
            v-if="!loading && objects.length === 0"
            description="Storage is empty"
          />

          <div v-else class="explorer-container">
            <!-- Folders List -->
            <div v-if="folderStructure.folders.length > 0" class="mb-4">
              <div class="section-header">
                <div class="i-carbon-folder text-orange-600" />
                <span>Folders ({{ folderStructure.folders.length }})</span>
              </div>
              <div class="folder-grid">
                <div
                  v-for="folder in folderStructure.folders"
                  :key="folder"
                  class="folder-item"
                >
                  <div
                    @click="navigateToFolder(folder)"
                    class="flex items-center gap-2 flex-1 cursor-pointer"
                  >
                    <div class="i-carbon-folder text-4xl text-orange-500" />
                    <span class="folder-name">{{ folder }}</span>
                  </div>
                  <el-button
                    size="small"
                    type="danger"
                    text
                    @click.stop="confirmDeleteFolder(folder)"
                  >
                    Delete
                  </el-button>
                </div>
              </div>
            </div>

            <!-- Files Table -->
            <div v-if="folderStructure.files.length > 0">
              <div class="section-header">
                <div class="i-carbon-document text-blue-600" />
                <span>Files ({{ folderStructure.files.length }})</span>
              </div>
              <el-table :data="folderStructure.files" stripe max-height="500">
                <el-table-column label="Name" min-width="300">
                  <template #default="{ row }">
                    <div class="flex items-center gap-2">
                      <div
                        :class="getFileIcon(row.name)"
                        class="text-lg text-gray-600 dark:text-gray-400"
                      />
                      <code class="text-sm font-mono">{{ row.name }}</code>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="Size" width="120" align="right">
                  <template #default="{ row }">
                    <span class="font-mono text-sm">{{
                      formatBytes(row.size)
                    }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="Storage Class" width="150">
                  <template #default="{ row }">
                    <el-tag size="small" type="info">
                      {{ row.storage_class }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="Last Modified" width="180">
                  <template #default="{ row }">
                    {{ formatDate(row.last_modified) }}
                  </template>
                </el-table-column>
                <el-table-column label="Actions" width="100" fixed="right">
                  <template #default="{ row }">
                    <el-button
                      size="small"
                      type="danger"
                      text
                      @click="confirmDeleteObject(row.key)"
                    >
                      Delete
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <!-- Empty folder message -->
            <el-empty
              v-if="
                folderStructure.folders.length === 0 &&
                folderStructure.files.length === 0
              "
              description="This folder is empty"
            />
          </div>
        </div>
      </el-card>

      <OrphanLakefsRepos
        class="mt-6"
        :token="adminStore.token"
        @error="
          ElMessage.error(
            $event.response?.data?.detail?.error || 'LakeFS audit failed',
          )
        "
      />
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.bucket-card {
  background-color: var(--bg-card);
  transition: all 0.3s ease;
  border: 2px solid var(--border-default);
}

.bucket-card:hover {
  border-color: var(--color-info);
  box-shadow: var(--shadow-md);
  transform: translateY(-4px);
  background: linear-gradient(135deg, var(--bg-hover) 0%, var(--bg-card) 100%);
}

.bucket-card h3 {
  color: var(--text-primary);
}

.bucket-card .text-xs {
  color: var(--text-secondary);
}

.bucket-card .text-sm {
  color: var(--text-secondary);
}

.bucket-card .font-semibold {
  color: var(--text-primary);
}

/* File Explorer Styles */
.breadcrumb-container {
  display: flex;
  align-items: center;
  padding: 16px;
  background-color: var(--bg-hover);
  border-radius: 8px;
  margin-bottom: 20px;
  border: 1px solid var(--border-default);
}

.breadcrumb-item {
  cursor: pointer;
  transition: all 0.2s ease;
  display: inline-flex;
  align-items: center;
}

.breadcrumb-item:hover {
  color: var(--color-info);
  transform: scale(1.05);
}

.breadcrumb-item span {
  font-weight: 500;
  color: var(--text-primary);
}

.explorer-container {
  margin-top: 16px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background-color: var(--bg-hover);
  border-radius: 8px;
  margin-bottom: 12px;
  font-weight: 600;
  color: var(--text-primary);
  border-left: 4px solid var(--color-info);
}

.folder-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.folder-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  background-color: var(--bg-card);
  border: 2px solid var(--border-default);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.3s ease;
  gap: 12px;
}

.folder-item:hover {
  border-color: var(--color-warning);
  background: linear-gradient(
    135deg,
    rgba(245, 158, 11, 0.1) 0%,
    var(--bg-card) 100%
  );
  transform: translateY(-4px);
  box-shadow: var(--shadow-md);
}

.folder-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
  text-align: center;
  word-break: break-word;
  max-width: 100%;
}

/* Table styling */
:deep(.el-table) {
  background-color: var(--bg-card);
  border-radius: 8px;
  overflow: hidden;
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

/* Breadcrumb styling */
:deep(.el-breadcrumb__inner) {
  display: flex;
  align-items: center;
  font-weight: 500;
  color: var(--text-primary);
}

:deep(.el-breadcrumb__inner:hover) {
  color: var(--color-info);
}
</style>
