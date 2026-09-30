<script setup>
import { ref, watch, onUnmounted } from "vue";
import {
  getQuota,
  setQuota,
  recalculateQuota,
  formatBytes,
  parseSize,
} from "@/utils/api";
import { ElMessage } from "element-plus";

const props = defineProps({
  namespace: {
    type: String,
    required: true,
  },
  isOrg: {
    type: Boolean,
    default: false,
  },
  token: {
    type: String,
    required: true,
  },
});

const quotaInfo = ref(null);
const loading = ref(false);
const editing = ref(false);
const recalculating = ref(false);
const debounceTimer = ref(null);

// Edit form
const editForm = ref({
  privateQuota: "",
  publicQuota: "",
});

async function loadQuota() {
  loading.value = true;
  try {
    quotaInfo.value = await getQuota(props.token, props.namespace, props.isOrg);

    // Update edit form with current values
    editForm.value.privateQuota =
      quotaInfo.value.private_quota_bytes !== null
        ? formatBytes(quotaInfo.value.private_quota_bytes)
        : "unlimited";
    editForm.value.publicQuota =
      quotaInfo.value.public_quota_bytes !== null
        ? formatBytes(quotaInfo.value.public_quota_bytes)
        : "unlimited";
  } catch (error) {
    console.error("Failed to load quota:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to load quota information",
    );
  } finally {
    loading.value = false;
  }
}

function debouncedLoadQuota() {
  // Clear existing timer
  if (debounceTimer.value) {
    clearTimeout(debounceTimer.value);
  }

  // Set new timer - wait 800ms after last input before loading
  debounceTimer.value = setTimeout(() => {
    if (props.namespace && props.token) {
      loadQuota();
    }
  }, 800);
}

async function handleSaveQuota() {
  try {
    const privateBytes = parseSize(editForm.value.privateQuota);
    const publicBytes = parseSize(editForm.value.publicQuota);

    await setQuota(
      props.token,
      props.namespace,
      {
        private_quota_bytes: privateBytes,
        public_quota_bytes: publicBytes,
      },
      props.isOrg,
    );

    ElMessage.success("Quota updated successfully");
    editing.value = false;
    loadQuota();
  } catch (error) {
    console.error("Failed to update quota:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to update quota",
    );
  }
}

async function handleRecalculate() {
  recalculating.value = true;
  try {
    const result = await recalculateQuota(
      props.token,
      props.namespace,
      props.isOrg,
    );
    ElMessage.success(
      result?.already_pending
        ? "A recount of this namespace is already scheduled"
        : "Recount scheduled; usage updates when it finishes",
    );
    loadQuota();
  } catch (error) {
    console.error("Failed to recalculate:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to recalculate storage",
    );
  } finally {
    recalculating.value = false;
  }
}

function formatPercentage(value) {
  return value !== null && value !== undefined ? value.toFixed(2) + "%" : "N/A";
}

// Watch for namespace changes with debounce
watch(
  () => [props.namespace, props.isOrg, props.token],
  (newVal, oldVal) => {
    // If token changed or isOrg changed (not just typing), load immediately
    const tokenChanged = newVal[2] !== oldVal?.[2];
    const isOrgChanged = newVal[1] !== oldVal?.[1];

    if (tokenChanged || isOrgChanged) {
      // Clear debounce timer and load immediately
      if (debounceTimer.value) {
        clearTimeout(debounceTimer.value);
      }
      if (props.namespace && props.token) {
        loadQuota();
      }
    } else {
      // Namespace changed (user typing), use debounce
      debouncedLoadQuota();
    }
  },
  { immediate: true },
);

// Cleanup on unmount
onUnmounted(() => {
  if (debounceTimer.value) {
    clearTimeout(debounceTimer.value);
  }
});
</script>

<template>
  <div>
    <el-card v-loading="loading">
      <template #header>
        <div class="flex justify-between items-center">
          <span class="text-xl font-semibold">
            Quota Information - {{ namespace }} ({{
              isOrg ? "Organization" : "User"
            }})
          </span>
          <div class="flex gap-2">
            <el-button
              v-if="!editing"
              type="primary"
              @click="editing = true"
              :icon="'Edit'"
              size="small"
            >
              Edit Quota
            </el-button>
            <el-button
              @click="handleRecalculate"
              :loading="recalculating"
              :icon="'Renew'"
              size="small"
            >
              Recount
            </el-button>
          </div>
        </div>
      </template>

      <div v-if="quotaInfo && !editing">
        <!-- Private Quota Section -->
        <div class="quota-section">
          <h3
            class="text-lg font-semibold mb-3 flex items-center text-gray-900 dark:text-gray-100"
          >
            <div
              class="i-carbon-locked mr-2 text-orange-600 dark:text-orange-400"
            />
            Private Repositories
          </h3>

          <el-descriptions :column="2" border class="mb-6">
            <el-descriptions-item label="Quota">
              {{ formatBytes(quotaInfo.private_quota_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Used">
              {{ formatBytes(quotaInfo.private_used_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Available">
              {{ formatBytes(quotaInfo.private_available_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Usage">
              {{ formatPercentage(quotaInfo.private_percentage_used) }}
            </el-descriptions-item>
          </el-descriptions>

          <!-- Progress Bar -->
          <el-progress
            v-if="quotaInfo.private_quota_bytes !== null"
            :percentage="Math.min(quotaInfo.private_percentage_used || 0, 100)"
            :status="
              quotaInfo.private_percentage_used > 90
                ? 'exception'
                : quotaInfo.private_percentage_used > 75
                  ? 'warning'
                  : 'success'
            "
          />
        </div>

        <el-divider />

        <!-- Public Quota Section -->
        <div class="quota-section">
          <h3
            class="text-lg font-semibold mb-3 flex items-center text-gray-900 dark:text-gray-100"
          >
            <div
              class="i-carbon-unlocked mr-2 text-cyan-600 dark:text-cyan-400"
            />
            Public Repositories
          </h3>

          <el-descriptions :column="2" border class="mb-6">
            <el-descriptions-item label="Quota">
              {{ formatBytes(quotaInfo.public_quota_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Used">
              {{ formatBytes(quotaInfo.public_used_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Available">
              {{ formatBytes(quotaInfo.public_available_bytes) }}
            </el-descriptions-item>
            <el-descriptions-item label="Usage">
              {{ formatPercentage(quotaInfo.public_percentage_used) }}
            </el-descriptions-item>
          </el-descriptions>

          <!-- Progress Bar -->
          <el-progress
            v-if="quotaInfo.public_quota_bytes !== null"
            :percentage="Math.min(quotaInfo.public_percentage_used || 0, 100)"
            :status="
              quotaInfo.public_percentage_used > 90
                ? 'exception'
                : quotaInfo.public_percentage_used > 75
                  ? 'warning'
                  : 'success'
            "
          />
        </div>

        <el-divider />

        <!-- Total Usage -->
        <div class="quota-section">
          <h3
            class="text-lg font-semibold mb-3 flex items-center text-gray-900 dark:text-gray-100"
          >
            <div
              class="i-carbon-data-volume mr-2 text-blue-600 dark:text-blue-400"
            />
            Total Storage
          </h3>

          <el-statistic
            title="Total Storage Used"
            :value="quotaInfo.total_used_bytes"
            :formatter="formatBytes"
          />
        </div>
      </div>

      <!-- Edit Form -->
      <div v-else-if="editing">
        <el-alert type="info" :closable="false" class="mb-6">
          <p class="text-sm">
            Enter quota values as human-readable sizes (e.g., "10GB", "500MB")
            or "unlimited" for no limit.
          </p>
          <p class="text-sm mt-2">
            Common sizes: 1GB, 10GB, 50GB, 100GB, 500GB, 1TB
          </p>
        </el-alert>

        <el-form :model="editForm" label-width="150px">
          <el-form-item label="Private Quota">
            <el-input
              v-model="editForm.privateQuota"
              placeholder="e.g., 10GB or unlimited"
            >
              <template #prepend>
                <div class="i-carbon-locked" />
              </template>
            </el-input>
            <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Current: {{ formatBytes(quotaInfo?.private_quota_bytes) }}
            </div>
          </el-form-item>

          <el-form-item label="Public Quota">
            <el-input
              v-model="editForm.publicQuota"
              placeholder="e.g., 50GB or unlimited"
            >
              <template #prepend>
                <div class="i-carbon-unlocked" />
              </template>
            </el-input>
            <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Current: {{ formatBytes(quotaInfo?.public_quota_bytes) }}
            </div>
          </el-form-item>

          <el-form-item>
            <el-button type="primary" @click="handleSaveQuota"
              >Save Changes</el-button
            >
            <el-button @click="editing = false">Cancel</el-button>
          </el-form-item>
        </el-form>
      </div>

      <el-skeleton v-else :rows="6" animated />
    </el-card>
  </div>
</template>

<style scoped>
.quota-section {
  margin-bottom: 24px;
}
</style>
