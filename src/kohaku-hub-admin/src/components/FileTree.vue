<script setup>
import { ref, watch, onMounted } from "vue";
import { getRepositoryFiles, formatBytes } from "@/utils/api";
import { ElMessage } from "element-plus";

const props = defineProps({
  repoType: {
    type: String,
    required: true,
  },
  namespace: {
    type: String,
    required: true,
  },
  name: {
    type: String,
    required: true,
  },
  token: {
    type: String,
    required: true,
  },
});

const files = ref([]);
const loading = ref(false);
const selectedRef = ref("main");

async function loadFiles() {
  loading.value = true;
  try {
    const response = await getRepositoryFiles(
      props.token,
      props.repoType,
      props.namespace,
      props.name,
      selectedRef.value,
    );
    files.value = response.files || [];
  } catch (error) {
    console.error("Failed to load files:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to load repository files",
    );
  } finally {
    loading.value = false;
  }
}

function getFileIcon(path) {
  const ext = path.split(".").pop().toLowerCase();
  const iconMap = {
    // Models
    safetensors: "i-carbon-model",
    bin: "i-carbon-model",
    pt: "i-carbon-model",
    pth: "i-carbon-model",
    ckpt: "i-carbon-model",
    onnx: "i-carbon-model",
    gguf: "i-carbon-model",

    // Config/JSON
    json: "i-carbon-code",
    yaml: "i-carbon-code",
    yml: "i-carbon-code",
    toml: "i-carbon-code",
    cfg: "i-carbon-code",

    // Text/Markdown
    md: "i-carbon-document",
    txt: "i-carbon-document",
    rst: "i-carbon-document",

    // Python
    py: "i-carbon-logo-python",

    // Archives
    zip: "i-carbon-archive",
    tar: "i-carbon-archive",
    gz: "i-carbon-archive",

    // Default
    default: "i-carbon-document",
  };

  return iconMap[ext] || iconMap.default;
}

function truncateSHA(sha) {
  return sha ? sha.substring(0, 16) + "..." : "-";
}

onMounted(() => {
  loadFiles();
});

// Watch for repository changes
watch(
  () => [props.repoType, props.namespace, props.name],
  () => {
    // Reset ref to main when switching repos
    selectedRef.value = "main";
    loadFiles();
  },
  { immediate: false },
);

// Watch for ref changes
watch(selectedRef, () => {
  loadFiles();
});
</script>

<template>
  <div class="file-tree-container">
    <div class="flex justify-between items-center mb-4">
      <div class="flex items-center gap-2">
        <span class="text-sm font-semibold">Branch/Ref:</span>
        <el-input
          v-model="selectedRef"
          placeholder="main"
          style="width: 200px"
          size="small"
        >
          <template #prepend>
            <div class="i-carbon-branch" />
          </template>
        </el-input>
        <el-button size="small" @click="loadFiles">
          <template #icon>
            <span class="i-carbon-renew" aria-hidden="true" />
          </template>
          Refresh
        </el-button>
      </div>
      <div class="text-sm text-gray-500">{{ files.length }} files</div>
    </div>

    <el-table
      :data="files"
      v-loading="loading"
      stripe
      style="width: 100%"
      max-height="500"
    >
      <el-table-column label="Path" min-width="300">
        <template #default="{ row }">
          <div class="flex items-center gap-2">
            <div :class="getFileIcon(row.path)" class="text-gray-600" />
            <span class="font-mono text-sm">{{ row.path }}</span>
            <el-tag
              v-if="row.is_lfs"
              type="warning"
              size="small"
              effect="plain"
            >
              LFS
            </el-tag>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="Size" width="120" align="right">
        <template #default="{ row }">
          <span class="font-mono text-sm">{{ formatBytes(row.size) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="Versions" width="100" align="center">
        <template #default="{ row }">
          <el-tag type="info" size="small">
            {{ row.version_count }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="SHA256" width="200">
        <template #default="{ row }">
          <code
            v-if="row.sha256"
            class="text-xs text-gray-600 dark:text-gray-400"
          >
            {{ truncateSHA(row.sha256) }}
          </code>
          <span v-else class="text-gray-400">-</span>
        </template>
      </el-table-column>
    </el-table>

    <div class="mt-4 p-3 bg-gray-50 dark:bg-gray-800 rounded">
      <div class="flex justify-between text-sm">
        <span class="text-gray-600 dark:text-gray-400">Total Files:</span>
        <span class="font-semibold">{{ files.length }}</span>
      </div>
      <div class="flex justify-between text-sm mt-1">
        <span class="text-gray-600 dark:text-gray-400">LFS Files:</span>
        <span class="font-semibold">
          {{ files.filter((f) => f.is_lfs).length }}
        </span>
      </div>
      <div class="flex justify-between text-sm mt-1">
        <span class="text-gray-600 dark:text-gray-400">Total Size:</span>
        <span class="font-semibold">
          {{ formatBytes(files.reduce((sum, f) => sum + f.size, 0)) }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.file-tree-container {
  width: 100%;
}
</style>
