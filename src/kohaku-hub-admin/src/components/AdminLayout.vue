<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAdminStore } from "@/stores/admin";
import { useThemeStore } from "@/stores/theme";
import { ElMessage } from "element-plus";
import GlobalSearch from "@/components/GlobalSearch.vue";

const router = useRouter();
const route = useRoute();
const adminStore = useAdminStore();
const themeStore = useThemeStore();

const globalSearchRef = ref(null);
const sidebarScrollRef = ref(null);
const canScrollUp = ref(false);
const canScrollDown = ref(false);
let sidebarResizeObserver;

function updateSidebarScrollState() {
  const sidebar = sidebarScrollRef.value;
  if (!sidebar) return;

  canScrollUp.value = sidebar.scrollTop > 1;
  canScrollDown.value =
    sidebar.scrollTop + sidebar.clientHeight < sidebar.scrollHeight - 1;
}

function scrollSidebar(direction) {
  sidebarScrollRef.value?.scrollBy({
    top: direction * sidebarScrollRef.value.clientHeight * 0.7,
    behavior: "smooth",
  });
}

onMounted(() => {
  updateSidebarScrollState();
  sidebarResizeObserver = new ResizeObserver(updateSidebarScrollState);
  sidebarResizeObserver.observe(sidebarScrollRef.value);
  sidebarResizeObserver.observe(sidebarScrollRef.value.firstElementChild);
});

onBeforeUnmount(() => {
  sidebarResizeObserver?.disconnect();
});

function handleLogout() {
  adminStore.logout();
  ElMessage.success("Logged out successfully");
  router.push("/login");
}

function openGlobalSearch() {
  if (globalSearchRef.value) {
    globalSearchRef.value.openDialog();
  }
}

const menuItems = [
  { path: "/", label: "Dashboard", icon: "i-carbon-dashboard" },
  { path: "/users", label: "Users", icon: "i-carbon-user-multiple" },
  { path: "/invitations", label: "Invitations", icon: "i-carbon-email" },
  { path: "/repositories", label: "Repositories", icon: "i-carbon-data-base" },
  { path: "/commits", label: "Commits", icon: "i-carbon-version" },
  { path: "/storage", label: "Storage", icon: "i-carbon-data-volume" },
  {
    path: "/fallback-sources",
    label: "Fallback Sources",
    icon: "i-carbon-connect",
  },
  {
    path: "/QuotaOverview",
    label: "Quota Overview",
    icon: "i-carbon-meter",
  },
  {
    path: "/health",
    label: "Health",
    icon: "i-carbon-activity",
  },
  {
    path: "/cache",
    label: "Cache",
    icon: "i-carbon-data-vis-1",
  },
  {
    path: "/tasks",
    label: "Background Tasks",
    icon: "i-carbon-task",
  },
  {
    path: "/credentials",
    label: "Credentials",
    icon: "i-carbon-password",
  },
  {
    path: "/DatabaseViewer",
    label: "Database",
    icon: "i-carbon-data-table",
  },
];
</script>

<template>
  <el-container class="admin-layout">
    <!-- Sidebar -->
    <el-aside width="250px" class="sidebar">
      <div class="sidebar-header">
        <div
          class="i-carbon-security text-2xl text-blue-600 dark:text-blue-400"
        />
        <h2 class="text-xl font-bold ml-2 text-gray-900 dark:text-gray-100">
          Admin Portal
        </h2>
      </div>

      <div class="sidebar-navigation">
        <div
          ref="sidebarScrollRef"
          class="sidebar-scroll"
          @scroll="updateSidebarScrollState"
        >
          <el-menu
            :default-active="route.path"
            router
            class="sidebar-menu"
            :background-color="themeStore.isDark ? '#1f1f1f' : '#ffffff'"
            :text-color="themeStore.isDark ? '#e0e0e0' : '#303133'"
            :active-text-color="'#409EFF'"
          >
            <el-menu-item
              v-for="item in menuItems"
              :key="item.path"
              :index="item.path"
            >
              <div :class="item.icon" class="mr-2" />
              <span>{{ item.label }}</span>
            </el-menu-item>
          </el-menu>
        </div>
        <button
          v-show="canScrollUp"
          type="button"
          class="sidebar-scroll-hint sidebar-scroll-hint-up"
          aria-label="Scroll navigation up"
          @click="scrollSidebar(-1)"
        >
          <div class="i-carbon-chevron-up" aria-hidden="true" />
        </button>
        <button
          v-show="canScrollDown"
          type="button"
          class="sidebar-scroll-hint sidebar-scroll-hint-down"
          aria-label="Scroll navigation down"
          @click="scrollSidebar(1)"
        >
          <div class="i-carbon-chevron-down" aria-hidden="true" />
        </button>
      </div>
    </el-aside>

    <!-- Main Content -->
    <el-container class="content-layout">
      <!-- Header -->
      <el-header class="header">
        <div class="header-title">
          <h1 class="text-xl font-semibold text-gray-900 dark:text-gray-100">
            KohakuHub Administration
          </h1>
        </div>

        <div class="header-actions">
          <el-button @click="openGlobalSearch" class="search-button">
            <div class="i-carbon-search text-lg" />
            <span class="ml-2 hidden sm:inline">Search</span>
            <el-tag size="small" effect="plain" class="ml-2 hidden md:inline">
              Ctrl+K
            </el-tag>
          </el-button>
          <el-button circle @click="themeStore.toggle()" class="mr-2">
            <div v-if="themeStore.isDark" class="i-carbon-moon text-lg" />
            <div v-else class="i-carbon-asleep text-lg" />
          </el-button>
          <el-button type="danger" @click="handleLogout">
            <template #icon>
              <span class="i-carbon-logout" aria-hidden="true" />
            </template>
            Logout
          </el-button>
        </div>
      </el-header>

      <!-- Content -->
      <el-main class="main-content">
        <slot />
      </el-main>
    </el-container>

    <!-- Global Search Modal -->
    <GlobalSearch ref="globalSearchRef" />
  </el-container>
</template>

<style scoped>
.admin-layout {
  --admin-header-height: 60px;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background-color: var(--bg-base);
}

.sidebar {
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  background-color: var(--bg-elevated);
  border-right: 1px solid var(--border-default);
  box-shadow: var(--shadow-sm);
  transition: all 0.2s ease;
}

.sidebar-header {
  height: var(--admin-header-height);
  display: flex;
  flex-shrink: 0;
  align-items: center;
  padding: 0 20px;
  border-bottom: 1px solid var(--border-light);
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}

.sidebar-header h2 {
  color: white !important;
}

.sidebar-navigation {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.sidebar-scroll {
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
  scroll-padding-block: 28px;
}

.sidebar-scroll::-webkit-scrollbar {
  display: none;
}

.sidebar-scroll-hint {
  position: absolute;
  left: 0;
  right: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 28px;
  border: none;
  color: var(--text-primary);
  cursor: pointer;
}

.sidebar-scroll-hint:hover {
  color: var(--color-info);
}

.sidebar-scroll-hint:focus-visible {
  outline: 2px solid var(--color-info);
  outline-offset: -2px;
}

.sidebar-scroll-hint-up {
  top: 0;
  background: linear-gradient(var(--bg-elevated) 60%, transparent);
}

.sidebar-scroll-hint-down {
  bottom: 0;
  background: linear-gradient(transparent, var(--bg-elevated) 40%);
}

.sidebar-menu {
  border-right: none;
  background-color: var(--bg-elevated) !important;
}

.content-layout {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.header {
  height: var(--admin-header-height);
  flex-shrink: 0;
  background-color: var(--bg-elevated);
  border-bottom: 1px solid var(--border-default);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  box-shadow: var(--shadow-sm);
  transition: all 0.2s ease;
}

.header-title {
  flex: 1;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.search-button {
  border-color: var(--border-default);
  background-color: var(--bg-hover);
  transition: all 0.2s ease;
}

.search-button:hover {
  border-color: var(--color-info);
  background-color: var(--bg-active);
}

.main-content {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background-color: var(--bg-base);
}
</style>
