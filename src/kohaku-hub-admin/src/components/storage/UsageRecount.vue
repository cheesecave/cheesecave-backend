<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { formatBytes, getUsageRecount, startUsageRecount } from "@/utils/api";

// Storage usage is kept up to date as repositories change; the recount sets
// every repository's exact usage from LakeFS and reports how far the kept
// numbers had drifted. Runs as a background task.
const props = defineProps({
  token: { type: String, required: true },
});
const emit = defineEmits(["error"]);

const POLL_MS = 3000;
const status = ref(null);
const starting = ref(false);
let timer = null;

const task = computed(() => status.value?.task || null);
const active = computed(() =>
  ["queued", "running"].includes(task.value?.status),
);
// Only rendered with a task
const percent = computed(() => {
  const { progress_done: done, progress_total: total } = task.value;
  return total ? Math.round((100 * done) / total) : 0;
});
const stats = computed(() => task.value.stats);
const drift = computed(() =>
  task.value.drift.map((row) => ({
    ...row,
    difference: row.after - row.before,
  })),
);
const statusType = {
  queued: "info",
  running: "primary",
  succeeded: "success",
  failed: "danger",
  cancelled: "warning",
};

async function load() {
  try {
    status.value = await getUsageRecount(props.token);
  } catch (error) {
    emit("error", error);
  }
  clearTimeout(timer);
  timer = active.value ? setTimeout(load, POLL_MS) : null;
}

async function start() {
  starting.value = true;
  try {
    const result = await startUsageRecount(props.token);
    ElMessage.success(
      result.already_pending
        ? "A recount is already scheduled"
        : `Recount scheduled (task #${result.task_id})`,
    );
    await load();
  } catch (error) {
    emit("error", error);
  }
  starting.value = false;
}

function formatTime(value) {
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}

function formatDifference(bytes) {
  return (
    (bytes > 0 ? "+" : bytes < 0 ? "-" : "") + formatBytes(Math.abs(bytes))
  );
}

onMounted(load);
onBeforeUnmount(() => clearTimeout(timer));
</script>

<template>
  <el-card data-testid="usage-recount">
    <template #header>
      <div class="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div class="font-bold">Storage usage recount</div>
          <div class="text-sm text-gray-500 dark:text-gray-400">
            Usage is kept up to date as repositories change. A recount sets
            every repository's usage from what it holds and reports how far the
            kept numbers had drifted. It runs once after upgrading; follow it
            under
            <router-link to="/tasks" class="text-blue-600"
              >Background Tasks</router-link
            >.
          </div>
        </div>
        <div class="flex gap-2">
          <el-button data-testid="usage-recount-refresh" @click="load()">
            <template #icon>
              <span class="i-carbon-renew" aria-hidden="true" />
            </template>
            Refresh
          </el-button>
          <el-button
            type="primary"
            :loading="starting"
            :disabled="active"
            data-testid="usage-recount-start"
            @click="start()"
          >
            <template #icon>
              <span class="i-carbon-renew" aria-hidden="true" />
            </template>
            {{ active ? "Running" : "Start recount" }}
          </el-button>
        </div>
      </div>
    </template>

    <div v-if="status" class="flex flex-col gap-3">
      <el-alert
        v-if="status.workers_online === 0"
        type="warning"
        :closable="false"
        show-icon
        data-testid="usage-recount-no-worker"
        title="No background worker is online: recounts wait in the queue until one runs."
      >
        Start a worker (khub-worker). Until one runs, a repository not counted
        yet since the upgrade does not follow changes to the regular files on
        its main branch; its LFS usage is kept either way.
      </el-alert>
      <div class="text-sm">
        Periodic recount:
        <el-tag
          size="small"
          :type="status.interval_hours > 0 ? 'success' : 'info'"
          data-testid="usage-recount-interval"
          >{{
            status.interval_hours > 0
              ? `every ${status.interval_hours} h`
              : "off"
          }}</el-tag
        >
      </div>
      <div v-if="task" data-testid="usage-recount-task">
        <div class="flex items-center gap-3 text-sm mb-2">
          <span>Task #{{ task.id }}</span>
          <el-tag size="small" :type="statusType[task.status]">{{
            task.status
          }}</el-tag>
          <span class="text-gray-500">{{ task.stage }}</span>
        </div>
        <el-progress
          :percentage="percent"
          :status="task.status === 'succeeded' ? 'success' : undefined"
        />
        <div class="text-xs text-gray-500 mt-1">
          {{ task.progress_done ?? 0 }} /
          {{ task.progress_total ?? "?" }} repositories · started
          {{ formatTime(task.created_at) }}
          <template v-if="task.finished_at">
            · finished {{ formatTime(task.finished_at) }}</template
          >
        </div>
        <div
          v-if="stats.repositories"
          class="flex gap-6 flex-wrap text-sm mt-3"
          data-testid="usage-recount-stats"
        >
          <div>
            Recounted: <b>{{ stats.repositories }}</b>
          </div>
          <div>
            Drifted: <b>{{ stats.drifted || 0 }}</b> ({{
              formatBytes(stats.drift_bytes || 0)
            }}
            in all)
          </div>
          <div v-if="stats.main_moved">
            Main moved meanwhile (caught up, not drift):
            <b>{{ stats.main_moved }}</b>
          </div>
          <div v-if="stats.busy">
            Changing meanwhile (recounted later): <b>{{ stats.busy }}</b>
          </div>
          <div v-if="stats.failed">
            Could not be read (retried later): <b>{{ stats.failed }}</b>
          </div>
        </div>
        <el-table
          v-if="drift.length"
          :data="drift"
          size="small"
          class="mt-3"
          data-testid="usage-recount-drift"
        >
          <el-table-column prop="repository" label="Largest drift" />
          <el-table-column label="Kept" width="140">
            <template #default="{ row }">{{
              formatBytes(row.before)
            }}</template>
          </el-table-column>
          <el-table-column label="Recounted" width="140">
            <template #default="{ row }">{{ formatBytes(row.after) }}</template>
          </el-table-column>
          <el-table-column label="Difference" width="140">
            <template #default="{ row }">{{
              formatDifference(row.difference)
            }}</template>
          </el-table-column>
        </el-table>
      </div>
      <el-empty
        v-else
        description="No recount has run yet."
        data-testid="usage-recount-none"
      />
    </div>
  </el-card>
</template>
