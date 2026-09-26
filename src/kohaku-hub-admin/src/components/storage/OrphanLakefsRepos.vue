<script setup>
import { computed, ref } from "vue";
import dayjs from "dayjs";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  listOrphanLakefsRepositories,
  purgeOrphanLakefsRepository,
} from "@/utils/api";

// Read-only audit of LakeFS repositories no repository points at (left by
// deletions before storage cleanup was scheduled on deletion, or by failed
// creates). Purging schedules a background task per repository.
const props = defineProps({
  token: { type: String, required: true },
});
const emit = defineEmits(["error"]);

const orphans = ref(null);
const scanning = ref(false);
const purging = ref(new Set());

const purgeable = computed(() =>
  (orphans.value || []).filter((orphan) => !orphan.purge_pending),
);

async function scan() {
  scanning.value = true;
  try {
    orphans.value = (await listOrphanLakefsRepositories(props.token)).orphans;
  } catch (error) {
    emit("error", error);
  } finally {
    scanning.value = false;
  }
}

async function confirm(message) {
  try {
    await ElMessageBox.confirm(message, "Purge orphaned LakeFS data", {
      type: "warning",
      confirmButtonText: "Purge",
      confirmButtonClass: "el-button--danger",
    });
    return true;
  } catch {
    return false;
  }
}

async function schedule(orphan) {
  purging.value = new Set(purging.value).add(orphan.id);
  let scheduled = false;
  try {
    await purgeOrphanLakefsRepository(props.token, orphan.id);
    orphan.purge_pending = true;
    scheduled = true;
  } catch (error) {
    emit("error", error);
  }
  const next = new Set(purging.value);
  next.delete(orphan.id);
  purging.value = next;
  return scheduled;
}

async function purgeOne(orphan) {
  const ok = await confirm(
    `Delete LakeFS repository ${orphan.id} and everything under ${orphan.storage_namespace}? This cannot be undone.`,
  );
  if (ok && (await schedule(orphan))) {
    ElMessage.success(`Purge of ${orphan.id} scheduled`);
  }
}

async function purgeAll() {
  const targets = purgeable.value;
  const ok = await confirm(
    `Delete ${targets.length} orphaned LakeFS repositories and their storage? This cannot be undone.`,
  );
  if (!ok) return;
  let scheduled = 0;
  for (const orphan of targets) {
    scheduled += (await schedule(orphan)) ? 1 : 0;
  }
  ElMessage.success(`Scheduled ${scheduled} purge(s)`);
}

function formatCreated(value) {
  // LakeFS reports creation time in Unix seconds.
  return value ? dayjs.unix(value).format("YYYY-MM-DD HH:mm:ss") : "—";
}
</script>

<template>
  <el-card data-testid="orphan-lakefs">
    <template #header>
      <div class="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <div class="font-bold">Orphaned LakeFS repositories</div>
          <div class="text-sm text-gray-500 dark:text-gray-400">
            LakeFS repositories no repository points at. Purging deletes the
            LakeFS repository and its S3 prefix in a background task; follow it
            under
            <router-link to="/tasks" class="text-blue-600"
              >Background Tasks</router-link
            >.
          </div>
        </div>
        <div class="flex gap-2">
          <el-button
            v-if="purgeable.length > 1"
            type="danger"
            plain
            data-testid="orphan-purge-all"
            @click="purgeAll()"
          >
            Purge all ({{ purgeable.length }})
          </el-button>
          <el-button
            type="primary"
            :loading="scanning"
            data-testid="orphan-scan"
            @click="scan()"
          >
            {{ orphans === null ? "Scan" : "Rescan" }}
          </el-button>
        </div>
      </div>
    </template>

    <el-empty
      v-if="orphans === null"
      description="Scan LakeFS to find repositories no repository points at."
      data-testid="orphan-not-scanned"
    />
    <el-empty
      v-else-if="orphans.length === 0"
      description="No orphaned LakeFS repositories."
      data-testid="orphan-none"
    />
    <el-table v-else :data="orphans" data-testid="orphan-table">
      <el-table-column label="LakeFS repository" min-width="300">
        <template #default="{ row }">
          <span class="font-mono">{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Created" width="180">
        <template #default="{ row }">{{
          formatCreated(row.created_at)
        }}</template>
      </el-table-column>
      <el-table-column label="Storage namespace" min-width="280">
        <template #default="{ row }">
          <span class="font-mono text-xs">{{ row.storage_namespace }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Status" width="160">
        <template #default="{ row }">
          <el-tag v-if="row.purge_pending" type="warning" size="small"
            >purge scheduled</el-tag
          >
          <el-tag v-else type="danger" size="small">orphaned</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="Actions" width="110" fixed="right">
        <template #default="{ row }">
          <el-button
            link
            type="danger"
            :disabled="row.purge_pending"
            :loading="purging.has(row.id)"
            :data-testid="`orphan-purge-${row.id}`"
            @click="purgeOne(row)"
          >
            Purge
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>
