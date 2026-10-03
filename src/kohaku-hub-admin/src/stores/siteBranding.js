import { acceptHMRUpdate, defineStore } from "pinia";
import { onScopeDispose, ref } from "vue";
import {
  CACHE_KEY,
  DEFAULT_BRANDING,
  applyDocumentBranding,
  fetchBranding,
  normalizeBranding,
  parseCachedBranding,
  readCachedBranding,
  saveCachedBranding,
} from "../../../shared/site-branding.js";

export const useSiteBrandingStore = defineStore("siteBranding", () => {
  const branding = ref(readCachedBranding());
  let revision = 0;

  function apply(value) {
    const next = normalizeBranding(value);
    if (!next) throw new Error("Invalid site branding response");
    revision += 1;
    branding.value = next;
    saveCachedBranding(next);
    applyDocumentBranding(next, { admin: true });
  }

  async function refresh() {
    const startedAt = revision;
    try {
      const next = await fetchBranding();
      if (revision === startedAt) apply(next);
    } catch {
      // The cached branding and packaged defaults remain usable offline.
    }
  }

  function handleStorage(event) {
    if (event.key !== CACHE_KEY && event.key !== null) return;
    const next = parseCachedBranding(event.newValue);
    if (!next && event.newValue !== null) return;
    revision += 1;
    branding.value = next || { ...DEFAULT_BRANDING };
    applyDocumentBranding(branding.value, { admin: true });
  }

  window.addEventListener("storage", handleStorage);
  onScopeDispose(() => window.removeEventListener("storage", handleStorage));
  applyDocumentBranding(branding.value, { admin: true });

  return { branding, apply, refresh };
});

if (import.meta.hot) {
  import.meta.hot.accept(
    acceptHMRUpdate(useSiteBrandingStore, import.meta.hot),
  );
}
