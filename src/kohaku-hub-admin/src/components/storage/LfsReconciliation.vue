<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import dayjs from "dayjs";
import { ElMessage } from "element-plus";
import { getLfsReconciliation, startLfsReconciliation } from "@/utils/api";

// Reconciles the database with the LFS objects every branch head links, so
// garbage collection keep windows account for data written by earlier
// versions. Idempotent and non-destructive; runs as a background task.
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
const stats = computed(() =>
  Object.entries(task.value.stats).map(([key, value]) => ({
    key: key.replaceAll("_", " "),
    value,
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
    status.value = await getLfsReconciliation(props.token);
  } catch (error) {
    emit("error", error);
  }
  clearTimeout(timer);
  timer = active.value ? setTimeout(load, POLL_MS) : null;
}

async function start() {
  starting.value = true;
  try {
    const result = await startLfsReconciliation(props.token);
    ElMessage.success(
      result.already_pending
        ? "A reconciliation is already scheduled"
        : `Reconciliation scheduled (task #${result.task_id})`,
    );
    await load();
  } catch (error) {
    emit("error", error);
  }
  starting.value = false;
}

function formatTime(value) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "never";
}

onMounted(load);
onBeforeUnmount(() => clearTimeout(timer));
</script>

<template>
  <el-card data-testid="lfs-reconciliation">
    <template #header>
      <div class="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div class="font-bold">LFS reference reconciliation</div>
          <div class="text-sm text-gray-500 dark:text-gray-400">
            Records every LFS object the head of any branch links, so garbage
            collection keeps it, and corrects the default branch's file rows. It
            only adds or corrects rows: running it again changes nothing. Follow
            it under
            <router-link to="/tasks" class="text-blue-600"
              >Background Tasks</router-link
            >.
          </div>
        </div>
        <div class="flex gap-2">
          <el-button data-testid="lfs-reconcile-refresh" @click="load()">
            Refresh
          </el-button>
          <el-button
            type="primary"
            :loading="starting"
            :disabled="active"
            data-testid="lfs-reconcile-start"
            @click="start()"
          >
            {{ active ? "Running" : "Start reconciliation" }}
          </el-button>
        </div>
      </div>
    </template>

    <div v-if="status" class="flex flex-col gap-3">
      <div class="flex gap-6 flex-wrap text-sm">
        <div>
          Last completed:
          <span class="font-medium" data-testid="lfs-reconciled-at">{{
            formatTime(status.reconciled_at)
          }}</span>
        </div>
        <div>
          Auto GC:
          <el-tag
            size="small"
            :type="status.auto_gc ? 'success' : 'info'"
            data-testid="lfs-auto-gc"
            >{{ status.auto_gc ? "on" : "off" }}</el-tag
          >
        </div>
      </div>
      <el-alert
        v-if="status.auto_gc && !status.reconciled_at"
        type="warning"
        :closable="false"
        show-icon
        data-testid="lfs-collection-waiting"
        title="LFS garbage collection waits until a reconciliation has completed once."
      />
      <div v-if="task" data-testid="lfs-reconcile-task">
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
        <el-table
          v-if="stats.length"
          :data="stats"
          size="small"
          class="mt-3"
          data-testid="lfs-reconcile-stats"
        >
          <el-table-column prop="key" label="Counter" />
          <el-table-column prop="value" label="Value" width="160" />
        </el-table>
      </div>
      <el-empty
        v-else
        description="No reconciliation has run yet."
        data-testid="lfs-reconcile-none"
      />
    </div>
  </el-card>
</template>
