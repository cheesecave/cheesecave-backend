<script setup>
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import AdminPage from "@/components/AdminPage.vue";
import AdminPageHeader from "@/components/AdminPageHeader.vue";
import { useAdminStore } from "@/stores/admin";
import {
  createRegisterInvitation,
  listInvitations,
  deleteInvitation,
} from "@/utils/api";
import { ElMessage, ElMessageBox } from "element-plus";
import { copyToClipboard } from "@/utils/clipboard";
import dayjs from "dayjs";

const router = useRouter();
const adminStore = useAdminStore();
const dialogVisible = ref(false);
const generatedLink = ref("");
const invitations = ref([]);
const loading = ref(false);

// Filter
const filterAction = ref("all");

// Create invitation form
const createForm = ref({
  org_id: null,
  role: "member",
  max_usage: 10,
  expires_days: 7,
});

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function handleCreateInvitation() {
  if (!checkAuth()) return;

  try {
    const payload = {
      org_id: createForm.value.org_id || null,
      role: createForm.value.role,
      max_usage: createForm.value.max_usage,
      expires_days: createForm.value.expires_days,
    };

    const response = await createRegisterInvitation(adminStore.token, payload);
    generatedLink.value = response.invitation_link;
    ElMessage.success("Registration invitation created successfully");
  } catch (error) {
    console.error("Failed to create invitation:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to create invitation",
    );
  }
}

async function handleCopyLink() {
  const success = await copyToClipboard(generatedLink.value);
  if (success) {
    ElMessage.success("Invitation link copied to clipboard");
  } else {
    ElMessage.error("Failed to copy link");
  }
}

function resetForm() {
  createForm.value = {
    org_id: null,
    role: "member",
    max_usage: 10,
    expires_days: 7,
  };
  generatedLink.value = "";
}

function closeDialog() {
  dialogVisible.value = false;
  resetForm();
  loadInvitations();
}

async function loadInvitations() {
  if (!checkAuth()) return;

  loading.value = true;
  try {
    const actionFilter =
      filterAction.value === "all" ? null : filterAction.value;
    const response = await listInvitations(adminStore.token, {
      action: actionFilter,
      limit: 100,
    });
    invitations.value = response.invitations;
  } catch (error) {
    console.error("Failed to load invitations:", error);
    ElMessage.error("Failed to load invitations");
  } finally {
    loading.value = false;
  }
}

async function handleDeleteInvitation(invitation) {
  try {
    await ElMessageBox.confirm(
      `Delete this ${invitation.action === "register_account" ? "registration" : "organization"} invitation?

Usage: ${invitation.usage_count} / ${invitation.max_usage === -1 ? "Unlimited" : invitation.max_usage || 1}
${invitation.email ? `Email: ${invitation.email}` : "General link"}

This action cannot be undone.`,
      "Confirm Delete",
      {
        confirmButtonText: "Delete",
        cancelButtonText: "Cancel",
        type: "warning",
      },
    );

    await deleteInvitation(adminStore.token, invitation.token);
    ElMessage.success("Invitation deleted successfully");
    loadInvitations();
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      console.error("Failed to delete invitation:", error);
      ElMessage.error("Failed to delete invitation");
    }
  }
}

async function handleCopyInvitationLink(token, action) {
  const baseUrl = window.location.origin;
  let link;

  if (action === "register_account") {
    link = `${baseUrl}/register?invitation=${token}`;
  } else {
    link = `${baseUrl}/invite/${token}`;
  }

  const success = await copyToClipboard(link);
  if (success) {
    ElMessage.success("Invitation link copied to clipboard");
  } else {
    ElMessage.error("Failed to copy link");
  }
}

function formatDate(dateStr) {
  return dayjs(dateStr).format("YYYY-MM-DD HH:mm");
}

function getActionLabel(action) {
  switch (action) {
    case "register_account":
      return "Register Account";
    case "join_org":
      return "Join Organization";
    default:
      return action;
  }
}

function getStatusType(invitation) {
  if (!invitation.is_available) return "danger";
  if (invitation.usage_count > 0) return "success";
  return "info";
}

function getStatusLabel(invitation) {
  if (!invitation.is_available) {
    return invitation.error_message || "Unavailable";
  }
  if (invitation.usage_count === 0) {
    return "Pending";
  }
  return `Used ${invitation.usage_count}x`;
}

const filteredInvitations = computed(() => {
  if (!invitations.value) return [];
  if (filterAction.value === "all") return invitations.value;
  return invitations.value.filter((inv) => inv.action === filterAction.value);
});

onMounted(() => {
  checkAuth();
  loadInvitations();
});
</script>

<template>
  <AdminLayout>
    <AdminPage>
      <AdminPageHeader
        title="Registration Invitations"
        subtitle="Create and manage invitation links for registration and organizations."
      >
        <template #actions>
          <el-button type="primary" @click="dialogVisible = true">
            <template #icon>
              <span class="i-carbon-add" aria-hidden="true" />
            </template>
            Generate Invitation
          </el-button>
        </template>
      </AdminPageHeader>

      <!-- Filter -->
      <el-card class="mb-4">
        <div class="flex items-center gap-4">
          <span class="font-semibold">Filter by Type:</span>
          <el-radio-group v-model="filterAction" @change="loadInvitations">
            <el-radio-button label="all">All Invitations</el-radio-button>
            <el-radio-button label="register_account"
              >Registration</el-radio-button
            >
            <el-radio-button label="join_org">Organization</el-radio-button>
          </el-radio-group>
        </div>
      </el-card>

      <!-- Invitations List -->
      <el-card class="mb-6">
        <template #header>
          <div class="flex items-center justify-between">
            <span class="font-semibold">All Invitations</span>
            <el-button size="small" @click="loadInvitations" :loading="loading">
              <template #icon>
                <span class="i-carbon-renew" aria-hidden="true" />
              </template>
              Refresh
            </el-button>
          </div>
        </template>

        <el-table
          :data="filteredInvitations"
          v-loading="loading"
          stripe
          style="width: 100%"
        >
          <el-table-column prop="action" label="Type" width="150">
            <template #default="{ row }">
              <el-tag
                :type="
                  row.action === 'register_account' ? 'success' : 'primary'
                "
                size="small"
              >
                {{ getActionLabel(row.action) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="Details" min-width="200">
            <template #default="{ row }">
              <div class="text-sm">
                <div v-if="row.email" class="font-medium">{{ row.email }}</div>
                <div v-else-if="row.org_name" class="font-medium">
                  {{ row.org_name }} ({{ row.role }})
                </div>
                <div v-else class="text-gray-500">General Link</div>
                <div v-if="row.is_reusable" class="text-xs text-gray-500 mt-1">
                  <el-tag size="small" type="success">Reusable</el-tag>
                  Usage: {{ row.usage_count }} /
                  {{ row.max_usage === -1 ? "∞" : row.max_usage || 1 }}
                </div>
              </div>
            </template>
          </el-table-column>

          <el-table-column
            prop="creator_username"
            label="Created By"
            width="120"
          />

          <el-table-column prop="created_at" label="Created" width="160">
            <template #default="{ row }">
              {{ formatDate(row.created_at) }}
            </template>
          </el-table-column>

          <el-table-column prop="expires_at" label="Expires" width="160">
            <template #default="{ row }">
              {{ formatDate(row.expires_at) }}
            </template>
          </el-table-column>

          <el-table-column label="Status" width="150">
            <template #default="{ row }">
              <el-tag :type="getStatusType(row)" size="small">
                {{ getStatusLabel(row) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="Actions" width="200" fixed="right">
            <template #default="{ row }">
              <el-button
                size="small"
                @click="handleCopyInvitationLink(row.token, row.action)"
              >
                <template #icon>
                  <span class="i-carbon-copy" aria-hidden="true" />
                </template>
                Copy Link
              </el-button>
              <el-button
                size="small"
                type="danger"
                @click="handleDeleteInvitation(row)"
              >
                <template #icon>
                  <span class="i-carbon-trash-can" aria-hidden="true" />
                </template>
                Delete
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-empty
          v-if="!loading && filteredInvitations.length === 0"
          description="No invitations found"
        />
      </el-card>

      <!-- Info Card -->
      <el-card class="mb-6">
        <template #header>
          <div class="flex items-center gap-2">
            <div class="i-carbon-information text-xl" />
            <span class="font-semibold">About Registration Invitations</span>
          </div>
        </template>

        <div class="space-y-3 text-sm">
          <p>
            Registration invitations allow users to create accounts on this
            KohakuHub instance. You can optionally add them to an organization
            automatically upon registration.
          </p>

          <div
            class="bg-blue-50 dark:bg-blue-900/20 p-3 rounded border border-blue-200 dark:border-blue-800"
          >
            <div class="font-semibold text-blue-800 dark:text-blue-200 mb-1">
              Invitation-Only Mode
            </div>
            <div class="text-blue-700 dark:text-blue-300">
              Set
              <code class="px-1 py-0.5 bg-blue-100 dark:bg-blue-900 rounded"
                >KOHAKU_HUB_INVITATION_ONLY=true</code
              >
              to disable public registration and require invitations.
            </div>
          </div>

          <div class="pt-2">
            <div class="font-semibold mb-2">Invitation Types:</div>
            <ul
              class="list-disc list-inside space-y-1 text-gray-700 dark:text-gray-300"
            >
              <li>
                <strong>One-time:</strong> Can be used once (max_usage not set)
              </li>
              <li>
                <strong>Limited:</strong> Can be used N times (max_usage =
                1-100)
              </li>
              <li>
                <strong>Unlimited:</strong> Can be used infinitely (max_usage =
                -1)
              </li>
            </ul>
          </div>
        </div>
      </el-card>

      <!-- Create Invitation Dialog -->
      <el-dialog
        v-model="dialogVisible"
        title="Generate Registration Invitation"
        width="600px"
        @close="resetForm"
      >
        <el-form :model="createForm" label-width="180px">
          <el-form-item label="Maximum Usage">
            <el-select v-model="createForm.max_usage" style="width: 100%">
              <el-option label="One-time use" :value="null" />
              <el-option label="1 use" :value="1" />
              <el-option label="5 uses" :value="5" />
              <el-option label="10 uses" :value="10" />
              <el-option label="25 uses" :value="25" />
              <el-option label="50 uses" :value="50" />
              <el-option label="100 uses" :value="100" />
              <el-option label="Unlimited" :value="-1" />
            </el-select>
            <div class="text-xs text-gray-500 mt-1">
              How many times this invitation can be used
            </div>
          </el-form-item>

          <el-form-item label="Expires In">
            <el-select v-model="createForm.expires_days" style="width: 100%">
              <el-option label="1 day" :value="1" />
              <el-option label="3 days" :value="3" />
              <el-option label="7 days" :value="7" />
              <el-option label="14 days" :value="14" />
              <el-option label="30 days" :value="30" />
              <el-option label="90 days" :value="90" />
              <el-option label="365 days" :value="365" />
            </el-select>
            <div class="text-xs text-gray-500 mt-1">
              Days until invitation expires
            </div>
          </el-form-item>

          <el-divider />

          <el-form-item label="Auto-Join Organization">
            <el-input
              v-model.number="createForm.org_id"
              type="number"
              placeholder="Leave empty for no auto-join"
            />
            <div class="text-xs text-gray-500 mt-1">
              Optional: Organization ID to join after registration
            </div>
          </el-form-item>

          <el-form-item v-if="createForm.org_id" label="Role in Organization">
            <el-select v-model="createForm.role" style="width: 100%">
              <el-option label="Visitor (Read-only)" value="visitor" />
              <el-option label="Member" value="member" />
              <el-option label="Admin" value="admin" />
            </el-select>
            <div class="text-xs text-gray-500 mt-1">
              Role assigned when user joins organization
            </div>
          </el-form-item>

          <!-- Generated Link Display -->
          <el-form-item v-if="generatedLink" label="Invitation Link">
            <el-input :value="generatedLink" readonly class="font-mono text-sm">
              <template #append>
                <el-button @click="handleCopyLink">
                  <div class="i-carbon-copy" />
                </el-button>
              </template>
            </el-input>
            <div class="text-xs text-green-600 mt-1">
              ✓ Invitation created! Share this link with users.
            </div>
          </el-form-item>
        </el-form>

        <template #footer>
          <el-button @click="closeDialog">
            {{ generatedLink ? "Done" : "Cancel" }}
          </el-button>
          <el-button
            v-if="!generatedLink"
            type="primary"
            @click="handleCreateInvitation"
          >
            Generate Invitation
          </el-button>
        </template>
      </el-dialog>
    </AdminPage>
  </AdminLayout>
</template>

<style scoped>
code {
  font-family: "SF Mono", "Monaco", "Consolas", monospace;
  font-size: 13px;
  color: var(--text-primary);
  background-color: var(--bg-hover);
  padding: 2px 6px;
  border-radius: 4px;
}

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

:deep(.text-gray-500) {
  color: var(--text-secondary);
}

:deep(.text-green-600) {
  color: var(--color-success);
  font-weight: 600;
}
</style>
