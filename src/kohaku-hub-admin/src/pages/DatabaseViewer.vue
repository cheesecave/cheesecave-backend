<script setup>
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import AdminLayout from "@/components/AdminLayout.vue";
import { useAdminStore } from "@/stores/admin";
import {
  listDatabaseTables,
  getDatabaseQueryTemplates,
  executeDatabaseQuery,
} from "@/utils/api";
import { ElMessage } from "element-plus";

const router = useRouter();
const adminStore = useAdminStore();

const tables = ref([]);
const templates = ref([]);
const queryText = ref("");
const queryResults = ref(null);
const loadingTables = ref(false);
const executing = ref(false);
const selectedTable = ref(null);

// PostgreSQL reserved keywords that need quoting
const reservedKeywords = [
  "user",
  "session",
  "commit",
  "table",
  "index",
  "group",
  "order",
];

function checkAuth() {
  if (!adminStore.token) {
    router.push("/login");
    return false;
  }
  return true;
}

async function loadTables() {
  if (!checkAuth()) return;

  loadingTables.value = true;
  try {
    const response = await listDatabaseTables(adminStore.token);
    tables.value = response.tables || [];
  } catch (error) {
    console.error("Failed to load tables:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Failed to load database tables",
    );
  } finally {
    loadingTables.value = false;
  }
}

async function loadTemplates() {
  if (!checkAuth()) return;

  try {
    const response = await getDatabaseQueryTemplates(adminStore.token);
    templates.value = response.templates || [];
  } catch (error) {
    console.error("Failed to load templates:", error);
  }
}

async function executeQuery() {
  if (!checkAuth()) return;

  if (!queryText.value || queryText.value.trim().length === 0) {
    ElMessage.warning("Please enter a SQL query");
    return;
  }

  executing.value = true;
  queryResults.value = null;

  try {
    const result = await executeDatabaseQuery(
      adminStore.token,
      queryText.value,
    );
    queryResults.value = result;

    // Debug logging
    console.log("Query results:", {
      columns: result.columns,
      columnCount: result.columns.length,
      rowCount: result.count,
      firstRow: result.rows[0],
    });

    if (result.truncated) {
      ElMessage.warning(
        "Results truncated to 1000 rows. Please add LIMIT to your query.",
      );
    } else {
      ElMessage.success(
        `Query executed: ${result.count} rows, ${result.columns.length} columns`,
      );
    }
  } catch (error) {
    console.error("Query execution failed:", error);
    ElMessage.error(
      error.response?.data?.detail?.error || "Query execution failed",
    );
  } finally {
    executing.value = false;
  }
}

function loadTemplate(template) {
  queryText.value = template.sql;
  ElMessage.success(`Loaded template: ${template.name}`);
}

function selectTable(table) {
  selectedTable.value = table;
  // Quote table name to handle reserved keywords (e.g., "user")
  queryText.value = `SELECT * FROM "${table.name}" LIMIT 100;`;
}

function isReservedKeyword(tableName) {
  return reservedKeywords.includes(tableName.toLowerCase());
}

function exportCSV() {
  if (!queryResults.value || !queryResults.value.rows) {
    ElMessage.warning("No results to export");
    return;
  }

  const { columns, rows } = queryResults.value;

  // Build CSV
  const csv = [
    columns.join(","), // Header
    ...rows.map((row) =>
      columns.map((col) => JSON.stringify(row[col] ?? "")).join(","),
    ),
  ].join("\n");

  // Download
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `query_results_${Date.now()}.csv`;
  a.click();
  URL.revokeObjectURL(url);

  ElMessage.success("Results exported to CSV");
}

function exportJSON() {
  if (!queryResults.value || !queryResults.value.rows) {
    ElMessage.warning("No results to export");
    return;
  }

  const json = JSON.stringify(queryResults.value.rows, null, 2);

  // Download
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `query_results_${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);

  ElMessage.success("Results exported to JSON");
}

onMounted(() => {
  loadTables();
  loadTemplates();
});
</script>

<template>
  <AdminLayout>
    <div class="page-container">
      <div class="flex justify-between items-center mb-6">
        <h1 class="text-3xl font-bold text-gray-900 dark:text-gray-100">
          Database Viewer
        </h1>
        <el-alert type="warning" :closable="false" show-icon>
          <span class="text-sm"
            >Read-only mode - Only SELECT queries allowed</span
          >
        </el-alert>
      </div>

      <div class="database-viewer-container">
        <div class="sidebar-section">
          <!-- Tables List -->
          <el-card class="mb-4">
            <template #header>
              <div class="flex items-center gap-2">
                <div class="i-carbon-table text-blue-600" />
                <span class="font-bold">Tables</span>
              </div>
            </template>

            <div v-loading="loadingTables" class="tables-list">
              <div
                v-for="table in tables"
                :key="table.name"
                class="table-item"
                :class="{ selected: selectedTable?.name === table.name }"
                @click="selectTable(table)"
              >
                <div class="flex items-center gap-2">
                  <div
                    class="i-carbon-data-table text-gray-600 dark:text-gray-400"
                  />
                  <div class="flex-1">
                    <div class="flex items-center gap-2">
                      <span class="font-mono text-sm font-semibold">
                        {{ table.name }}
                      </span>
                      <el-tag
                        v-if="isReservedKeyword(table.name)"
                        type="warning"
                        size="small"
                        effect="plain"
                      >
                        use quotes
                      </el-tag>
                    </div>
                    <div class="text-xs text-gray-500 dark:text-gray-400">
                      {{ table.column_count }} columns
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </el-card>

          <!-- Query Templates -->
          <el-card>
            <template #header>
              <div class="flex items-center gap-2">
                <div class="i-carbon-template text-green-600" />
                <span class="font-bold">Templates</span>
              </div>
            </template>

            <div class="templates-list">
              <div
                v-for="template in templates"
                :key="template.name"
                class="template-item"
                @click="loadTemplate(template)"
              >
                <div class="font-semibold text-sm">{{ template.name }}</div>
                <div class="text-xs text-gray-500">
                  {{ template.description }}
                </div>
              </div>
            </div>
          </el-card>
        </div>

        <div class="editor-section">
          <!-- Query Editor -->
          <el-card class="mb-4">
            <template #header>
              <div class="flex justify-between items-center">
                <span class="font-bold">Query Editor</span>
                <div class="flex gap-2">
                  <el-button size="small" @click="queryText = ''">
                    <template #icon>
                      <span class="i-carbon-erase" aria-hidden="true" />
                    </template>
                    Clear
                  </el-button>
                  <el-button
                    type="primary"
                    size="small"
                    @click="executeQuery"
                    :loading="executing"
                  >
                    <template #icon>
                      <span class="i-carbon-play" aria-hidden="true" />
                    </template>
                    Execute Query
                  </el-button>
                </div>
              </div>
            </template>

            <el-input
              v-model="queryText"
              type="textarea"
              :rows="10"
              placeholder="Enter your SELECT query here...&#10;&#10;Example:&#10;SELECT * FROM user WHERE is_org = 0 LIMIT 10;"
              class="sql-editor"
            />

            <div class="mt-2 space-y-1">
              <div class="text-xs text-gray-600 dark:text-gray-400">
                <div class="i-carbon-information inline-block mr-1" />
                Only SELECT queries allowed. Max 1000 rows. Use LIMIT clause for
                better performance.
              </div>
              <div class="text-xs text-orange-600 dark:text-orange-400">
                <div class="i-carbon-warning inline-block mr-1" />
                <strong>Important:</strong> Use double quotes for table names
                with reserved keywords:
                <code class="ml-1 px-1 bg-gray-100 dark:bg-gray-800 rounded"
                  >SELECT * FROM "user"</code
                >
              </div>
            </div>
          </el-card>

          <!-- Query Results -->
          <el-card v-if="queryResults">
            <template #header>
              <div class="flex justify-between items-center">
                <div>
                  <span class="font-bold">Results</span>
                  <el-tag class="ml-2" type="info">
                    {{ queryResults.columns.length }} columns
                  </el-tag>
                  <el-tag class="ml-2" type="success">
                    {{ queryResults.count }} rows
                  </el-tag>
                  <el-tag
                    v-if="queryResults.truncated"
                    class="ml-2"
                    type="warning"
                  >
                    Truncated to 1000
                  </el-tag>
                </div>
                <div class="flex gap-2">
                  <el-button size="small" @click="exportCSV">
                    <template #icon>
                      <span class="i-carbon-download" aria-hidden="true" />
                    </template>
                    Export CSV
                  </el-button>
                  <el-button size="small" @click="exportJSON">
                    <template #icon>
                      <span class="i-carbon-download" aria-hidden="true" />
                    </template>
                    Export JSON
                  </el-button>
                </div>
              </div>
            </template>

            <div v-if="queryResults.count === 0" class="text-center py-8">
              <el-empty description="Query returned no results" />
            </div>

            <div v-else>
              <!-- Column List (for debugging/info) -->
              <div class="column-info">
                <div>
                  <strong>Columns:</strong>
                  <span>{{ queryResults.columns.join(", ") }}</span>
                </div>
              </div>

              <div class="results-table-wrapper">
                <div class="results-table-container">
                  <el-table
                    :key="queryResults.columns.join(',')"
                    :data="queryResults.rows"
                    stripe
                    border
                    max-height="500"
                    fit
                  >
                    <el-table-column
                      v-for="column in queryResults.columns"
                      :key="column"
                      :prop="column"
                      :label="column"
                      min-width="120"
                      show-overflow-tooltip
                    >
                      <template #default="{ row }">
                        <div
                          class="cell-content"
                          :class="{ 'cell-null': row[column] === null }"
                        >
                          {{ row[column] !== null ? row[column] : "NULL" }}
                        </div>
                      </template>
                    </el-table-column>
                  </el-table>
                </div>
                <div class="scroll-hint" v-if="queryResults.columns.length > 5">
                  <div class="i-carbon-arrow-right" />
                  <span class="text-xs"
                    >Scroll horizontally to see more columns ({{
                      queryResults.columns.length
                    }}
                    total)</span
                  >
                </div>
              </div>
            </div>
          </el-card>

          <el-empty
            v-else
            description="Execute a query to see results"
            :image-size="120"
          />
        </div>
      </div>
    </div>
  </AdminLayout>
</template>

<style scoped>
.page-container {
  padding: 24px;
}

.database-viewer-container {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 24px;
  align-items: start;
  max-width: 100%;
  overflow-x: hidden;
}

.sidebar-section {
  position: sticky;
  top: 20px;
  min-width: 300px;
  max-width: 300px;
}

.editor-section {
  min-height: 600px;
  min-width: 0; /* Important: allows grid item to shrink below content size */
  overflow-x: hidden;
}

.tables-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 400px;
  overflow-y: auto;
  padding-right: 4px;
}

.tables-list::-webkit-scrollbar {
  width: 6px;
}

.tables-list::-webkit-scrollbar-track {
  background: var(--bg-hover);
  border-radius: 3px;
}

.tables-list::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 3px;
}

.table-item {
  padding: 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  background-color: var(--bg-hover);
  border: 2px solid transparent;
}

.table-item:hover {
  background-color: var(--bg-active);
  border-color: var(--border-light);
  transform: translateX(2px);
}

.table-item.selected {
  background: linear-gradient(
    135deg,
    rgba(59, 130, 246, 0.15) 0%,
    rgba(59, 130, 246, 0.05) 100%
  );
  border-color: var(--color-info);
  box-shadow: var(--shadow-sm);
}

.table-item .font-mono {
  color: var(--text-primary);
  font-weight: 600;
}

.table-item .text-xs {
  color: var(--text-secondary);
}

.templates-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 300px;
  overflow-y: auto;
  padding-right: 4px;
}

.templates-list::-webkit-scrollbar {
  width: 6px;
}

.templates-list::-webkit-scrollbar-track {
  background: var(--bg-hover);
  border-radius: 3px;
}

.templates-list::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 3px;
}

.template-item {
  padding: 12px;
  border: 2px solid var(--border-default);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  background-color: var(--bg-card);
}

.template-item:hover {
  border-color: var(--color-success);
  background: linear-gradient(
    135deg,
    rgba(16, 185, 129, 0.1) 0%,
    transparent 100%
  );
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
}

.template-item .font-semibold {
  color: var(--text-primary);
  margin-bottom: 4px;
}

.template-item .text-xs {
  color: var(--text-secondary);
}

.sql-editor :deep(textarea) {
  font-family: "Consolas", "Monaco", "Courier New", monospace;
  font-size: 14px;
  line-height: 1.6;
  background-color: var(--bg-hover);
  color: var(--text-primary);
  border-color: var(--border-default);
}

.sql-editor :deep(textarea:focus) {
  background-color: var(--bg-card);
  border-color: var(--color-info);
}

.results-table-wrapper {
  width: 100%;
  max-width: 100%;
  overflow: hidden;
}

.results-table-container {
  overflow-x: auto;
  overflow-y: visible;
  border-radius: 8px;
  border: 1px solid var(--border-default);
  max-width: 100%;
}

.results-table-container::-webkit-scrollbar {
  height: 12px;
}

.results-table-container::-webkit-scrollbar-track {
  background: var(--bg-hover);
  border-radius: 6px;
}

.results-table-container::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 6px;
  border: 2px solid var(--bg-hover);
}

.results-table-container::-webkit-scrollbar-thumb:hover {
  background: var(--text-tertiary);
}

.results-table-container :deep(.el-table) {
  background-color: var(--bg-card);
  min-width: 100%;
}

.results-table-container :deep(.el-table th) {
  background-color: var(--bg-hover);
  color: var(--text-primary);
  font-weight: 600;
}

.results-table-container :deep(.el-table td) {
  color: var(--text-primary);
}

.column-info {
  padding: 12px;
  background-color: var(--bg-hover);
  border-radius: 8px;
  border: 1px solid var(--border-light);
}

.column-info strong {
  color: var(--text-primary);
}

.column-info span {
  font-family: "Consolas", "Monaco", "Courier New", monospace;
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-word;
}

.scroll-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px;
  margin-top: 12px;
  background-color: var(--bg-hover);
  border-radius: 8px;
  color: var(--text-secondary);
  font-weight: 500;
}

.cell-content {
  font-family: "Consolas", "Monaco", "Courier New", monospace;
  font-size: 13px;
  white-space: nowrap;
  color: var(--text-primary);
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  padding: 2px 0;
}

/* Style for NULL values */
.cell-null {
  color: var(--text-tertiary) !important;
  font-style: italic;
  opacity: 0.7;
}

/* Ensure cards don't overflow */
.editor-section :deep(.el-card) {
  max-width: 100%;
  overflow: hidden;
}

.editor-section :deep(.el-card__body) {
  max-width: 100%;
  overflow-x: hidden;
}

@media (max-width: 1024px) {
  .database-viewer-container {
    grid-template-columns: 1fr;
  }

  .sidebar-section {
    position: static;
    max-width: 100%;
  }
}
</style>
