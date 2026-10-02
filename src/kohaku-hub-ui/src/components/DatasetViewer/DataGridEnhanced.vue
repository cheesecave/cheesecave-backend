<script setup>
import { looksLikeImageUrl } from "@/utils/media-types";
import { ref, computed } from "vue";

const props = defineProps({
  columns: {
    type: Array,
    required: true,
  },
  rows: {
    type: Array,
    required: true,
  },
  truncated: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["row-selected"]);

// Sorting
const sortColumn = ref(null);
const sortDirection = ref("asc");

// Row detail
const selectedRowIndex = ref(null);

// Media modal
const showMediaModal = ref(false);
const modalMediaUrl = ref("");
const modalMediaType = ref("image"); // "image" or "video"

// Max cell length before truncation
const MAX_CELL_LENGTH = 100;

// Sorting logic (MUST be defined before virtual scrolling!)
const sortedRows = computed(() => {
  if (!sortColumn.value) return props.rows;

  const colIndex = props.columns.indexOf(sortColumn.value);
  if (colIndex === -1) return props.rows;

  const sorted = [...props.rows].sort((a, b) => {
    const aVal = a[colIndex];
    const bVal = b[colIndex];

    // Handle null/undefined
    if (aVal == null) return 1;
    if (bVal == null) return -1;

    // Numeric comparison
    if (typeof aVal === "number" && typeof bVal === "number") {
      return aVal - bVal;
    }

    // String comparison
    return String(aVal).localeCompare(String(bVal));
  });

  return sortDirection.value === "desc" ? sorted.reverse() : sorted;
});

// Simple infinite scroll - only adds rows, never removes
const ROWS_PER_BATCH = 100;
const displayedRowCount = ref(100);

const displayedRows = computed(() => {
  return sortedRows.value.slice(0, displayedRowCount.value);
});

const hasMore = computed(() => {
  return displayedRowCount.value < sortedRows.value.length;
});

function handleScroll(event) {
  const element = event.target;
  const scrollBottom =
    element.scrollHeight - element.scrollTop - element.clientHeight;

  // Load more when within 200px of bottom
  if (scrollBottom < 200 && hasMore.value) {
    displayedRowCount.value = Math.min(
      displayedRowCount.value + ROWS_PER_BATCH,
      sortedRows.value.length,
    );
  }
}

// Toggle sort
function toggleSort(column) {
  if (sortColumn.value === column) {
    sortDirection.value = sortDirection.value === "asc" ? "desc" : "asc";
  } else {
    sortColumn.value = column;
    sortDirection.value = "asc";
  }
  // Reset displayed count when sorting
  displayedRowCount.value = 100;
}

// Format cell value
function formatValue(value) {
  if (value == null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// Check if cell is long
function isCellLong(value) {
  const formatted = formatValue(value);
  return formatted.length > MAX_CELL_LENGTH;
}

// Truncate cell value
function truncateValue(value) {
  const formatted = formatValue(value);
  if (formatted.length <= MAX_CELL_LENGTH) return formatted;
  return formatted.substring(0, MAX_CELL_LENGTH) + "...";
}

// Check if value is an image URL or base64
function isImage(value) {
  return looksLikeImageUrl(value);
}

// Check if value is a video URL
function isVideo(value) {
  if (!value) return false;
  const str = String(value);

  // Check for HTTP(S) video URLs
  if (str.startsWith("http://") || str.startsWith("https://")) {
    const lowerStr = str.toLowerCase();
    const videoExtensions = [".mp4", ".webm", ".ogg", ".mov", ".avi", ".mkv"];
    return videoExtensions.some((ext) => lowerStr.includes(ext));
  }

  return false;
}

// Get image/video URL
function getMediaUrl(value) {
  const str = String(value);
  if (str.startsWith("data:image/")) return str;
  if (str.startsWith("http://") || str.startsWith("https://")) return str;
  // If it's base64 without prefix, add it
  if (str.length > 100 && !str.includes(" ")) {
    return `data:image/png;base64,${str}`;
  }
  return null;
}

// Toggle row detail
function toggleRowDetail(index) {
  if (selectedRowIndex.value === index) {
    selectedRowIndex.value = null;
    emit("row-selected", null);
  } else {
    selectedRowIndex.value = index;
    emit("row-selected", index);
  }
}

// Open media in modal
function openMediaModal(value, type) {
  modalMediaUrl.value = getMediaUrl(value);
  modalMediaType.value = type;
  showMediaModal.value = true;
}

// Close media modal
function closeMediaModal() {
  showMediaModal.value = false;
  modalMediaUrl.value = "";
}
</script>

<template>
  <div class="data-grid-enhanced">
    <!-- Truncation warning -->
    <div
      v-if="truncated"
      class="warning p-3 bg-yellow-50 dark:bg-yellow-900/20 border-b border-yellow-200 dark:border-yellow-700"
    >
      <span class="text-yellow-800 dark:text-yellow-200">
        Showing first {{ rows.length }} rows. File contains more data.
      </span>
    </div>

    <!-- Data table with infinite scroll -->
    <div
      class="table-wrapper overflow-auto"
      @scroll="handleScroll"
      style="max-height: 600px"
    >
      <table class="data-table w-full border-collapse">
        <thead class="sticky top-0 bg-gray-50 dark:bg-gray-800 z-10">
          <tr>
            <th
              class="column-header px-2 py-3 text-left text-sm font-semibold border-b-2 border-gray-200 dark:border-gray-700"
              style="width: 40px"
            >
              #
            </th>
            <th
              v-for="column in columns"
              :key="column"
              class="column-header px-4 py-3 text-left text-sm font-semibold border-b-2 border-gray-200 dark:border-gray-700 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700"
              @click="toggleSort(column)"
            >
              <div class="flex items-center gap-2">
                <span>{{ column }}</span>
                <span
                  v-if="sortColumn === column"
                  class="sort-indicator text-blue-500"
                >
                  {{ sortDirection === "asc" ? "↑" : "↓" }}
                </span>
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          <!-- Only render visible rows -->
          <tr
            v-for="(row, index) in displayedRows"
            :key="index"
            class="hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer"
            @click="toggleRowDetail(index)"
            :class="{
              'bg-blue-50 dark:bg-blue-900/20': selectedRowIndex === index,
            }"
          >
            <td
              class="cell px-2 py-2 text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700"
            >
              {{ index + 1 }}
            </td>
            <td
              v-for="(value, colIndex) in row"
              :key="colIndex"
              class="cell px-4 py-2 text-sm border-b border-gray-200 dark:border-gray-700"
              :class="{
                'text-blue-600 dark:text-blue-400': isCellLong(value),
              }"
            >
              <!-- Image thumbnail -->
              <div v-if="isImage(value)" class="flex items-center gap-2">
                <img
                  :src="getMediaUrl(value)"
                  alt="Thumbnail"
                  class="w-16 h-16 object-cover rounded border border-gray-300 dark:border-gray-600 cursor-pointer hover:opacity-80"
                  @click.stop="openMediaModal(value, 'image')"
                />
                <span
                  class="text-xs text-gray-500 truncate max-w-[100px]"
                  :title="String(value)"
                >
                  {{ String(value).split("/").pop() }}
                </span>
              </div>

              <!-- Video thumbnail -->
              <div v-else-if="isVideo(value)" class="flex items-center gap-2">
                <div
                  class="w-16 h-16 flex items-center justify-center bg-gray-200 dark:bg-gray-700 rounded border border-gray-300 dark:border-gray-600 cursor-pointer hover:opacity-80"
                  @click.stop="openMediaModal(value, 'video')"
                >
                  <div
                    class="i-carbon-play-filled text-2xl text-gray-600 dark:text-gray-400"
                  />
                </div>
                <span
                  class="text-xs text-gray-500 truncate max-w-[100px]"
                  :title="String(value)"
                >
                  {{ String(value).split("/").pop() }}
                </span>
              </div>

              <!-- Regular text value -->
              <span v-else>{{ truncateValue(value) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Infinite scroll indicator -->
    <div
      v-if="hasMore"
      class="p-3 text-center border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800"
    >
      <div class="text-xs text-gray-600 dark:text-gray-400">
        Showing {{ displayedRowCount }} of {{ sortedRows.length }} rows - scroll
        down for more
      </div>
    </div>

    <!-- Empty state -->
    <div
      v-if="rows.length === 0"
      class="empty-state p-8 text-center text-gray-600 dark:text-gray-400"
    >
      No rows to display
    </div>

    <!-- Media Modal -->
    <el-dialog
      v-model="showMediaModal"
      width="90%"
      top="5vh"
      @close="closeMediaModal"
    >
      <template #header>
        <div class="flex items-center justify-between">
          <span class="text-lg font-semibold">
            {{ modalMediaType === "image" ? "Image Preview" : "Video Preview" }}
          </span>
        </div>
      </template>

      <!-- Media container with max height -->
      <div class="media-container" style="max-height: 70vh; overflow: auto">
        <!-- Image -->
        <div v-if="modalMediaType === 'image'" class="text-center">
          <img
            :src="modalMediaUrl"
            alt="Full size preview"
            class="max-w-full h-auto object-contain mx-auto"
            style="max-height: 65vh"
          />
        </div>

        <!-- Video -->
        <div v-else-if="modalMediaType === 'video'" class="text-center">
          <video
            :src="modalMediaUrl"
            controls
            class="max-w-full h-auto mx-auto"
            style="max-height: 65vh"
          >
            Your browser does not support the video tag.
          </video>
        </div>
      </div>

      <!-- URL info -->
      <div
        class="mt-3 p-2 bg-gray-50 dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-700"
      >
        <div
          class="text-xs font-semibold text-gray-600 dark:text-gray-400 mb-1"
        >
          URL:
        </div>
        <div
          class="text-xs font-mono text-gray-900 dark:text-gray-100 break-all"
        >
          {{ modalMediaUrl }}
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.table-wrapper {
  max-height: 600px;
}

.data-table {
  font-variant-numeric: tabular-nums;
}

.cell {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.column-header {
  user-select: none;
}

.sort-indicator {
  font-weight: bold;
}

.detail-row {
  border-left: 4px solid #3b82f6;
}

.detail-content {
  max-height: 600px;
  overflow-y: auto;
  /* Force detail content to fit viewport width, not table width */
  max-width: calc(100vw - 200px);
}

.detail-field {
  transition: all 0.15s;
}

.detail-field:hover {
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}
</style>
