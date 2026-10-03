<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import AdminLayout from "@/components/AdminLayout.vue";
import { useAdminStore } from "@/stores/admin";
import { useSiteBrandingStore } from "@/stores/siteBranding";
import { getGifLoop } from "../../../shared/site-branding.js";
import {
  getSiteBranding,
  updateSiteBranding,
  uploadSiteBrandingAsset,
  updateSiteBrandingAssetAnimation,
  resetSiteBrandingAsset,
} from "@/utils/api";

const router = useRouter();
const adminStore = useAdminStore();
const siteBrandingStore = useSiteBrandingStore();
const draft = reactive({ site_name: "", footer_description: "" });
const playback = reactive({ header_logo: true, favicon: true });
const fileInputs = {};
const savedPlayback = computed(() => ({
  header_logo: getGifLoop(siteBrandingStore.branding.header_logo),
  favicon: getGifLoop(siteBrandingStore.branding.favicon),
}));
const loaded = ref(false);
const busy = ref(false);
const errorMessage = ref("");
const disabled = computed(() => busy.value || !loaded.value);
const assets = [
  {
    key: "header_logo",
    label: "Header logo",
    description:
      "Shown in the site header. Upload separately from the favicon.",
    fallback: "/admin/images/logo-square.svg",
  },
  {
    key: "favicon",
    label: "Favicon",
    description: "Shown in browser tabs and bookmarks.",
    fallback: "/admin/favicon.svg",
  },
];
const accept =
  ".svg,.png,.jpg,.jpeg,.webp,.gif,.ico,image/svg+xml,image/png,image/jpeg,image/webp,image/gif,image/x-icon,image/vnd.microsoft.icon";

function checkAuth() {
  if (adminStore.token) return true;
  loaded.value = false;
  router.push("/login");
  return false;
}

function showError(error, fallback) {
  if ([401, 403].includes(error.response?.status)) {
    loaded.value = false;
    adminStore.logout();
    router.push("/login");
    errorMessage.value = "Invalid admin token. Please login again.";
  } else {
    const detail = error.response?.data?.detail;
    errorMessage.value = Array.isArray(detail)
      ? detail.map((item) => item.msg || String(item)).join("; ")
      : String(detail || error.message || fallback);
  }
  ElMessage.error(errorMessage.value);
}

function copyText(branding) {
  draft.site_name = branding.site_name;
  draft.footer_description = branding.footer_description;
}

function copyPlayback(asset) {
  playback[asset] = savedPlayback.value[asset] ?? true;
}

async function loadBranding() {
  if (busy.value || !checkAuth()) return;
  busy.value = true;
  loaded.value = false;
  errorMessage.value = "";
  try {
    siteBrandingStore.apply(await getSiteBranding(adminStore.token));
    copyText(siteBrandingStore.branding);
    assets.forEach((asset) => copyPlayback(asset.key));
    loaded.value = true;
  } catch (error) {
    showError(error, "Failed to load site branding");
  } finally {
    busy.value = false;
  }
}

async function saveText() {
  if (disabled.value || !checkAuth()) return;
  const siteName = draft.site_name.trim();
  if (
    !siteName ||
    Array.from(siteName).length > 100 ||
    Array.from(draft.footer_description).length > 2000
  ) {
    showError(
      new Error(
        "Enter a site name of 1–100 characters and a footer description of at most 2000 characters.",
      ),
    );
    return;
  }
  busy.value = true;
  errorMessage.value = "";
  try {
    siteBrandingStore.apply(
      await updateSiteBranding(adminStore.token, {
        site_name: siteName,
        footer_description: draft.footer_description,
      }),
    );
    copyText(siteBrandingStore.branding);
    ElMessage.success("Site name and footer description saved");
  } catch (error) {
    showError(error, "Failed to save site branding");
  } finally {
    busy.value = false;
  }
}

function chooseAsset(asset) {
  if (disabled.value || !checkAuth()) return;
  fileInputs[asset]?.click();
}

async function uploadAsset(asset, event) {
  const input = event.target;
  const file = input.files?.[0];
  input.value = "";
  if (!file || disabled.value || !checkAuth()) return;
  if (!/\.(svg|png|jpe?g|webp|gif|ico)$/i.test(file.name)) {
    showError(new Error("Choose an SVG, PNG, JPEG, WebP, GIF or ICO image."));
    return;
  }
  if (file.size > 2 * 1024 * 1024) {
    showError(new Error("Images must be 2 MiB or smaller."));
    return;
  }
  await mutateAsset(
    () =>
      uploadSiteBrandingAsset(adminStore.token, asset, file, playback[asset]),
    "Image uploaded",
    asset,
  );
}

async function resetAsset(asset) {
  if (disabled.value || !checkAuth()) return;
  await mutateAsset(
    () => resetSiteBrandingAsset(adminStore.token, asset),
    "Default image restored",
    asset,
  );
}

async function savePlayback(asset) {
  if (disabled.value || savedPlayback.value[asset] === null || !checkAuth())
    return;
  await mutateAsset(
    () =>
      updateSiteBrandingAssetAnimation(
        adminStore.token,
        asset,
        playback[asset],
      ),
    "GIF playback saved",
    asset,
  );
}

async function mutateAsset(action, message, asset) {
  busy.value = true;
  errorMessage.value = "";
  try {
    siteBrandingStore.apply(await action());
    copyPlayback(asset);
    ElMessage.success(message);
  } catch (error) {
    showError(error, "Failed to update image");
  } finally {
    busy.value = false;
  }
}

onMounted(loadBranding);
</script>

<template>
  <AdminLayout>
    <div class="branding-page">
      <div class="branding-heading">
        <div>
          <h2 class="text-2xl font-bold">Site Branding</h2>
          <p class="branding-help">
            Customize the site name, header logo, favicon and footer
            description.
          </p>
        </div>
        <el-button :disabled="busy" @click="loadBranding">Reload</el-button>
      </div>

      <p v-if="errorMessage" class="branding-error" role="alert">
        {{ errorMessage }}
      </p>
      <p v-if="!loaded" class="branding-help" role="status">
        {{
          busy
            ? "Loading site branding…"
            : "Load the current settings before editing."
        }}
      </p>

      <el-card class="branding-card">
        <template #header
          ><h3 class="text-lg font-semibold">Site name and footer</h3></template
        >
        <form @submit.prevent="saveText">
          <fieldset :disabled="disabled" class="branding-fields">
            <label for="site-name">Site name</label>
            <input
              id="site-name"
              v-model="draft.site_name"
              type="text"
              required
            />
            <label for="footer-description">Footer description</label>
            <textarea
              id="footer-description"
              v-model="draft.footer_description"
              rows="4"
            />
            <p class="branding-help">
              Plain text, up to 2000 characters. Leave blank to hide the
              description. Other KohakuHub introduction text is unchanged.
            </p>
            <div>
              <el-button
                type="primary"
                native-type="submit"
                :disabled="disabled"
                aria-label="Save site name and footer"
                >Save</el-button
              >
            </div>
          </fieldset>
        </form>
      </el-card>

      <div class="branding-assets">
        <el-card v-for="asset in assets" :key="asset.key" class="branding-card">
          <template #header
            ><h3 class="text-lg font-semibold">{{ asset.label }}</h3></template
          >
          <p class="branding-help">{{ asset.description }}</p>
          <div
            class="branding-preview"
            :class="{ 'favicon-preview': asset.key === 'favicon' }"
          >
            <img
              :src="siteBrandingStore.branding[asset.key] || asset.fallback"
              :alt="`${asset.label} preview`"
            />
          </div>
          <p class="branding-help">
            {{
              siteBrandingStore.branding[asset.key]
                ? "Custom image"
                : "Default image"
            }}
          </p>
          <input
            :id="asset.key"
            :ref="(input) => (fileInputs[asset.key] = input)"
            type="file"
            hidden
            :accept="accept"
            :disabled="disabled"
            @change="uploadAsset(asset.key, $event)"
          />
          <el-button
            :disabled="disabled"
            :aria-label="`Upload ${asset.label.toLowerCase()}`"
            @click="chooseAsset(asset.key)"
            >Upload</el-button
          >
          <p class="branding-help">
            SVG, PNG, JPEG, WebP, GIF or ICO. Maximum 2 MiB. SVG must be
            self-contained and static. GIF animation is preserved.
          </p>
          <label :for="`${asset.key}-playback`" class="branding-upload-label">
            GIF playback
          </label>
          <select
            :id="`${asset.key}-playback`"
            v-model="playback[asset.key]"
            :disabled="disabled"
            class="branding-playback"
          >
            <option :value="true">Loop forever</option>
            <option :value="false">Play once</option>
          </select>
          <p class="branding-help">
            Select before uploading a GIF, or save playback for the current GIF.
            <span v-if="asset.key === 'favicon'">
              Favicon animation depends on browser support.
            </span>
          </p>
          <div class="branding-actions">
            <el-button
              v-if="savedPlayback[asset.key] !== null"
              type="primary"
              :aria-label="`Save ${asset.label.toLowerCase()} playback`"
              :disabled="
                disabled || playback[asset.key] === savedPlayback[asset.key]
              "
              @click="savePlayback(asset.key)"
              >Save</el-button
            >
            <el-button
              type="danger"
              :aria-label="`Restore ${asset.label.toLowerCase()}`"
              :disabled="disabled || !siteBrandingStore.branding[asset.key]"
              @click="resetAsset(asset.key)"
              >Restore</el-button
            >
          </div>
        </el-card>
      </div>
      <p class="branding-help">
        Saved branding is cached in visitors' browsers. If the backend is
        unavailable, the last saved branding or the packaged defaults remain
        visible.
      </p>
    </div>
  </AdminLayout>
</template>

<style scoped>
.branding-page {
  max-width: 1100px;
  margin: 0 auto;
  color: var(--text-primary);
}
.branding-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
}
.branding-card {
  margin-bottom: 24px;
}
.branding-help {
  color: var(--text-secondary);
  margin: 8px 0 16px;
  line-height: 1.5;
}
.branding-error {
  padding: 12px;
  margin-bottom: 16px;
  color: var(--color-danger, #dc2626);
  border: 1px solid currentColor;
  border-radius: 8px;
}
.branding-fields {
  display: flex;
  flex-direction: column;
  gap: 12px;
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.branding-fields label,
.branding-upload-label {
  font-weight: 600;
}
.branding-fields input,
.branding-fields textarea,
.branding-playback {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 10px 12px;
  background: var(--bg-base);
  color: var(--text-primary);
  font: inherit;
}
.branding-fields textarea {
  resize: vertical;
}
.branding-fields input:focus-visible,
.branding-fields textarea:focus-visible,
.branding-playback:focus-visible {
  outline: 2px solid var(--color-info);
  outline-offset: 2px;
}
.branding-fields:disabled,
input:disabled,
select:disabled {
  opacity: 0.6;
}
.branding-assets {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 24px;
}
.branding-preview {
  height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: var(--bg-base);
  border: 1px solid var(--border-default);
  border-radius: 8px;
}
.branding-preview img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
.favicon-preview img {
  max-width: 64px;
  max-height: 64px;
}
.branding-upload-label {
  display: block;
  margin: 16px 0 8px;
}
.branding-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.branding-actions :deep(.el-button) {
  margin-left: 0;
}
@media (max-width: 720px) {
  .branding-assets {
    grid-template-columns: 1fr;
    gap: 0;
  }
  .branding-heading {
    align-items: flex-start;
  }
}
</style>
