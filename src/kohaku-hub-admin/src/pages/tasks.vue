<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import AdminPage from "@/components/AdminPage.vue";
import AdminPageHeader from "@/components/AdminPageHeader.vue";
import TaskDetail from "@/components/tasks/TaskDetail.vue";
import TaskOverview from "@/components/tasks/TaskOverview.vue";
import TaskProgress from "@/components/tasks/TaskProgress.vue";
import TaskWorkers from "@/components/tasks/TaskWorkers.vue";
import { useAdminStore } from "@/stores/admin";
import {
  cancelTask,
  deleteTask,
  getTask,
  getTaskStats,
  listTasks,
  listWorkers,
  retryTask,
} from "@/utils/api";
import { ElMessage, ElMessageBox } from "element-plus";
import { formatDate } from "@/components/tasks/taskFormat.js";

const router = useRouter();
const adminStore = useAdminStore();

const STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"];
const STATUS_TAG = {
  queued: "info",
  running: "primary",
  succeeded: "success",
  failed: "danger",
  cancelled: "warning",
};
const HEALTH_TAG = {
  healthy: { type: "success", label: "Healthy" },
  degraded: { type: "warning", label: "Degraded" },
  unhealthy: { type: "danger", label: "Unhealthy" },
  idle: { type: "info", label: "Idle" },
};
const REFRESH_OPTIONS = [
  { label: "Off", value: 0 },
  { label: "Every 5 s", value: 5 },
  { label: "Every 15 s", value: 15 },
  { label: "Every 60 s", value: 60 },
];

const loading = ref(false);
const tasks = ref([]);
const total = ref(0);
const counts = ref(Object.fromEntries(STATUSES.map((s) => [s, 0])));
const kinds = ref([]);
const filterStatus = ref("");
const filterKind = ref("");
const currentPage = ref(1);
const pageSize = ref(20);
const refreshIntervalSeconds = ref(0);
const detail = ref(null);
const detailVisible = ref(false);
const activeTab = ref("overview");
const statsWindow = ref("1h");
const stats = ref(null);
const statsFailed = ref(false);
const roster = ref(null);
const includeInactiveWorkers = ref(false);
const highlightedWorker = ref(null);
let refreshTimer = null;

// Online and draining workers are the ones alive right now.
const activeWorkers = computed(() =>
  roster.value
    ? roster.value.counts.online + roster.value.counts.draining
    : null,
);

const health = computed(() =>
  stats.value ? HEALTH_TAG[stats.value.health.status] : null,
);

const statusOptions = computed(() => [
  { label: "All statuses", value: "" },
  ...STATUSES.map((s) => ({ label: s, value: s })),
]);
const kindOptions = computed(() => [
  { label: "All kinds", value: "" },
  ...kinds.value.map((k) => ({ label: k, value: k })),
]);

function handleError(error, fallback) {
  if (error.response?.status === 401 || error.response?.status === 403) {
    ElMessage.error("Invalid admin token. Please login again.");
    adminStore.logout();
    router.push("/login");
    return;
  }
  ElMessage.error(error.response?.data?.detail?.error || fallback);
}

async function loadTasks() {
  if (!adminStore.token) {
    router.push("/login");
    return;
  }
  loading.value = true;
  try {
    const response = await listTasks(adminStore.token, {
      status: filterStatus.value || undefined,
      kind: filterKind.value || undefined,
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    });
    tasks.value = response.tasks;
    total.value = response.total;
    counts.value = response.counts;
    kinds.value = response.kinds;
  } catch (error) {
    handleError(error, "Failed to load background tasks");
  } finally {
    loading.value = false;
  }
}

async function loadStats() {
  if (!adminStore.token) return; // loadTasks handles the redirect
  const requested = statsWindow.value;
  try {
    const data = await getTaskStats(adminStore.token, requested);
    // A slower response for a window the user already left must not win.
    if (requested !== statsWindow.value) return;
    stats.value = data;
    statsFailed.value = false;
  } catch (error) {
    statsFailed.value = true;
    handleError(error, "Failed to load task health");
  }
}

async function loadWorkers() {
  if (!adminStore.token) return; // loadTasks handles the redirect
  try {
    roster.value = await listWorkers(adminStore.token, {
      includeInactive: includeInactiveWorkers.value,
    });
  } catch (error) {
    handleError(error, "Failed to load workers");
  }
}

function showWorker(workerId) {
  highlightedWorker.value = workerId;
  detailVisible.value = false;
  activeTab.value = "workers";
}

function refreshAll() {
  loadTasks();
  loadStats();
  loadWorkers();
  if (detailVisible.value && detail.value) reloadDetail(detail.value.id);
}

// Keeps an open detail live (progress, timeline) on refresh; stays quiet
// if the task was discarded meanwhile.
async function reloadDetail(taskId) {
  try {
    const fresh = await getTask(adminStore.token, taskId);
    if (detailVisible.value && detail.value?.id === taskId)
      detail.value = fresh;
  } catch {
    // The next manual action reports errors.
  }
}

function applyFilters() {
  currentPage.value = 1;
  loadTasks();
}

// Status cards toggle their filter; anything picked from the overview opens
// the task list already filtered.
function selectStatus(status) {
  const toggleOff =
    activeTab.value === "tasks" && filterStatus.value === status;
  filterStatus.value = toggleOff ? "" : status;
  activeTab.value = "tasks";
  applyFilters();
}

function showStatus(status) {
  filterStatus.value = status;
  filterKind.value = "";
  activeTab.value = "tasks";
  applyFilters();
}

function showKind(kind) {
  filterKind.value = kind;
  filterStatus.value = "";
  activeTab.value = "tasks";
  applyFilters();
}

function handlePageChange(page) {
  currentPage.value = page;
  loadTasks();
}

async function openDetail(task) {
  try {
    detail.value = await getTask(adminStore.token, task.id);
    detailVisible.value = true;
  } catch (error) {
    handleError(error, "Failed to load task");
  }
}

async function confirmAction(message, title) {
  try {
    await ElMessageBox.confirm(message, title, { type: "warning" });
    return true;
  } catch {
    return false;
  }
}

async function handleRetry(task) {
  if (
    !(await confirmAction(
      `Requeue task #${task.id} (${task.kind})?`,
      "Retry task",
    ))
  ) {
    return;
  }
  try {
    await retryTask(adminStore.token, task.id);
    ElMessage.success(`Task #${task.id} requeued`);
    detailVisible.value = false;
    refreshAll();
  } catch (error) {
    handleError(error, "Failed to retry task");
  }
}

async function handleCancel(task) {
  const message =
    task.status === "running"
      ? `Ask the worker to stop task #${task.id} (${task.kind})? It gets ` +
        "a grace period to stop cleanly, then it is interrupted."
      : `Cancel task #${task.id} (${task.kind}) before it starts?`;
  if (!(await confirmAction(message, "Cancel task"))) {
    return;
  }
  try {
    const updated = await cancelTask(adminStore.token, task.id);
    ElMessage.success(
      updated.status === "cancelled"
        ? `Task #${task.id} cancelled`
        : `Cancellation of task #${task.id} requested`,
    );
    refreshAll();
  } catch (error) {
    handleError(error, "Failed to cancel task");
  }
}

async function handleDiscard(task) {
  if (
    !(await confirmAction(
      `Discard task #${task.id} (${task.kind}) with its timeline and logs? This cannot be undone.`,
      "Discard task",
    ))
  ) {
    return;
  }
  try {
    await deleteTask(adminStore.token, task.id);
    ElMessage.success(`Task #${task.id} discarded`);
    detailVisible.value = false;
    refreshAll();
  } catch (error) {
    handleError(error, "Failed to discard task");
  }
}

function canRetry(task) {
  return task.status === "failed" || task.status === "cancelled";
}

function canCancel(task) {
  return (
    task.status === "queued" ||
    (task.status === "running" && !task.cancel_requested)
  );
}

function canDiscard(task) {
  return task.status === "failed" || task.status === "cancelled";
}

// The one timestamp that matters for the row's current state.
function stateTime(task) {
  switch (task.status) {
    case "queued":
      return `due ${formatDate(task.run_after)}`;
    case "running":
      return `started ${formatDate(task.started_at)}`;
    default:
      return `finished ${formatDate(task.finished_at)}`;
  }
}

function truncate(text, maxLength = 80) {
  if (!text) return "";
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

function stopTimer() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

watch(refreshIntervalSeconds, (seconds) => {
  stopTimer();
  if (seconds > 0) {
    refreshTimer = setInterval(refreshAll, seconds * 1000);
  }
});

watch(statsWindow, loadStats);
watch(includeInactiveWorkers, loadWorkers);

onMounted(refreshAll);
onBeforeUnmount(stopTimer);
</script>

<template>
  <AdminLayout>
    <AdminPage>
      <AdminPageHeader>
        <template #title>
          Background Tasks
          <span
            v-if="health"
            class="health-chip"
            :class="health.type"
            data-testid="tasks-health"
          >
            <span class="health-dot" />{{ health.label }}
          </span>
          <button
            v-if="activeWorkers !== null"
            type="button"
            class="health-chip workers-chip"
            :class="activeWorkers ? 'success' : 'danger'"
            data-testid="tasks-workers-chip"
            @click="activeTab = 'workers'"
          >
            {{ activeWorkers }} worker(s) online
          </button>
        </template>
        <template #subtitle
          >Durable tasks executed by <code>khub-worker</code>. Tasks run at
          least once. Follow progress and logs, cancel queued or running tasks,
          and retry or discard failed ones here.</template
        >
        <template #actions>
          <el-select
            v-model="refreshIntervalSeconds"
            class="refresh-select"
            placeholder="Auto-refresh"
            data-testid="tasks-refresh-interval"
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
            @click="refreshAll()"
            data-testid="tasks-refresh"
          >
            <div class="i-carbon-renew mr-1" />
            Refresh
          </el-button>
        </template>
      </AdminPageHeader>

      <div class="status-cards mb-4">
        <button
          v-for="status in STATUSES"
          :key="status"
          type="button"
          class="status-card"
          :class="[
            `status-${status}`,
            { active: activeTab === 'tasks' && filterStatus === status },
          ]"
          :data-testid="`tasks-count-${status}`"
          @click="selectStatus(status)"
        >
          <span class="status-label"
            ><span class="status-dot" />{{ status }}</span
          >
          <span class="status-value">{{ counts[status] }}</span>
        </button>
      </div>

      <el-tabs v-model="activeTab" class="task-tabs">
        <el-tab-pane label="Overview" name="overview">
          <TaskOverview
            v-if="stats"
            v-model:window="statsWindow"
            :stats="stats"
            @select-kind="showKind"
            @select-status="showStatus"
          />
          <el-empty
            v-else
            :description="
              statsFailed
                ? 'Could not load task health'
                : 'Loading task health…'
            "
          >
            <el-button
              v-if="statsFailed"
              data-testid="tasks-stats-retry"
              @click="loadStats()"
            >
              Retry
            </el-button>
          </el-empty>
        </el-tab-pane>
        <el-tab-pane label="Tasks" name="tasks">
          <el-card shadow="never">
            <div class="flex gap-3 mb-4 flex-wrap">
              <el-select
                v-model="filterStatus"
                class="filter-select"
                placeholder="Status"
                data-testid="tasks-filter-status"
                @change="applyFilters"
              >
                <el-option
                  v-for="option in statusOptions"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
              <el-select
                v-model="filterKind"
                class="filter-select"
                placeholder="Kind"
                data-testid="tasks-filter-kind"
                @change="applyFilters"
              >
                <el-option
                  v-for="option in kindOptions"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </div>

            <el-empty
              v-if="!loading && tasks.length === 0"
              description="No background tasks"
              data-testid="tasks-empty"
            />
            <el-table
              v-else
              v-loading="loading"
              :data="tasks"
              data-testid="tasks-table"
            >
              <el-table-column prop="id" label="ID" width="70" />
              <el-table-column
                prop="kind"
                label="Kind"
                min-width="180"
                show-overflow-tooltip
              />
              <el-table-column label="Status" width="170">
                <template #default="{ row }">
                  <el-tag :type="STATUS_TAG[row.status]" size="small">
                    {{ row.status }}
                  </el-tag>
                  <el-tag
                    v-if="row.lease_expired"
                    type="warning"
                    size="small"
                    class="ml-1"
                    data-testid="tasks-lease-expired"
                  >
                    lease expired
                  </el-tag>
                  <el-tag
                    v-if="row.stalled"
                    type="danger"
                    size="small"
                    class="ml-1"
                    :data-testid="`tasks-stalled-${row.id}`"
                  >
                    stalled
                  </el-tag>
                  <el-tag
                    v-if="row.cancel_requested && row.status === 'running'"
                    type="warning"
                    size="small"
                    class="ml-1"
                    :data-testid="`tasks-cancelling-${row.id}`"
                  >
                    cancelling
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="Progress" min-width="200">
                <template #default="{ row }">
                  <TaskProgress :task="row" compact />
                </template>
              </el-table-column>
              <el-table-column label="Attempts" width="90">
                <template #default="{ row }">
                  {{ row.attempts }} / {{ row.max_attempts }}
                </template>
              </el-table-column>
              <el-table-column label="When" min-width="190">
                <template #default="{ row }">{{ stateTime(row) }}</template>
              </el-table-column>
              <el-table-column
                label="Last error"
                min-width="180"
                show-overflow-tooltip
              >
                <template #default="{ row }">
                  <span class="error-text">{{ truncate(row.last_error) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="Actions" width="230" fixed="right">
                <template #default="{ row }">
                  <el-button
                    link
                    type="primary"
                    :data-testid="`tasks-detail-${row.id}`"
                    @click="openDetail(row)"
                  >
                    Details
                  </el-button>
                  <el-button
                    v-if="canCancel(row)"
                    link
                    type="warning"
                    :data-testid="`tasks-cancel-${row.id}`"
                    @click="handleCancel(row)"
                  >
                    Cancel
                  </el-button>
                  <el-button
                    v-if="canRetry(row)"
                    link
                    type="warning"
                    :data-testid="`tasks-retry-${row.id}`"
                    @click="handleRetry(row)"
                  >
                    Retry
                  </el-button>
                  <el-button
                    v-if="canDiscard(row)"
                    link
                    type="danger"
                    :data-testid="`tasks-discard-${row.id}`"
                    @click="handleDiscard(row)"
                  >
                    Discard
                  </el-button>
                </template>
              </el-table-column>
            </el-table>

            <div class="flex justify-end mt-4">
              <el-pagination
                :current-page="currentPage"
                :page-size="pageSize"
                :total="total"
                layout="total, prev, pager, next"
                data-testid="tasks-pagination"
                @current-change="handlePageChange"
              />
            </div>
          </el-card>
        </el-tab-pane>
        <el-tab-pane label="Workers" name="workers">
          <el-card shadow="never">
            <TaskWorkers
              v-if="roster"
              v-model:include-inactive="includeInactiveWorkers"
              :roster="roster"
              :highlight="highlightedWorker"
              @select-task="openDetail({ id: $event })"
            />
          </el-card>
        </el-tab-pane>
      </el-tabs>

      <el-dialog
        v-model="detailVisible"
        :title="detail ? `Task #${detail.id}` : 'Task'"
        width="920px"
      >
        <TaskDetail
          v-if="detail"
          :key="detail.id"
          :task="detail"
          :token="adminStore.token"
          @error="handleError($event, 'Failed to load task logs')"
          @show-worker="showWorker"
        />
        <template #footer>
          <el-button
            v-if="detail && canCancel(detail)"
            type="warning"
            data-testid="tasks-detail-cancel"
            @click="handleCancel(detail)"
          >
            Cancel task
          </el-button>
          <el-button
            v-if="detail && canRetry(detail)"
            type="warning"
            data-testid="tasks-detail-retry"
            @click="handleRetry(detail)"
          >
            Retry
          </el-button>
          <el-button
            v-if="detail && canDiscard(detail)"
            type="danger"
            data-testid="tasks-detail-discard"
            @click="handleDiscard(detail)"
          >
            Discard
          </el-button>
          <el-button @click="detailVisible = false">Close</el-button>
        </template>
      </el-dialog>
    </AdminPage>
  </AdminLayout>
</template>

<style scoped>
.refresh-select {
  width: 160px;
}

.filter-select {
  width: 220px;
}

.status-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}

.status-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 14px 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-bg-color);
  cursor: pointer;
  text-align: left;
}

/* Each card borrows its status badge colour (info/primary/success/danger/warning)
   as an accent, so cards and table badges read as one legend. */
.status-card {
  --status-color: var(--el-color-info);
  --status-tint: var(--el-color-info-light-9);
  border-left: 3px solid var(--status-color);
}

.status-card.status-running {
  --status-color: var(--el-color-primary);
  --status-tint: var(--el-color-primary-light-9);
}

.status-card.status-succeeded {
  --status-color: var(--el-color-success);
  --status-tint: var(--el-color-success-light-9);
}

.status-card.status-failed {
  --status-color: var(--el-color-danger);
  --status-tint: var(--el-color-danger-light-9);
}

.status-card.status-cancelled {
  --status-color: var(--el-color-warning);
  --status-tint: var(--el-color-warning-light-9);
}

.status-card:hover {
  background: var(--status-tint);
}

.status-card.active {
  background: var(--status-tint);
  border-color: var(--status-color);
  box-shadow: 0 0 0 1px var(--status-color);
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 50%;
  background: var(--status-color);
}

.status-card .status-value {
  color: var(--status-color);
}

.health-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  --chip-color: var(--el-color-info);
  color: var(--chip-color);
  background: color-mix(in srgb, var(--chip-color) 12%, transparent);
}

.health-chip.success {
  --chip-color: var(--el-color-success);
}

.health-chip.warning {
  --chip-color: var(--el-color-warning);
}

.health-chip.danger {
  --chip-color: var(--el-color-danger);
}

.workers-chip {
  border: none;
  cursor: pointer;
  text-transform: none;
  letter-spacing: 0;
}

.health-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--chip-color);
}

.status-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.status-value {
  font-size: 24px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.error-text {
  color: var(--el-color-danger);
}
</style>
