import { acceptHMRUpdate, defineStore } from "pinia";
import {
  applyDocumentBranding,
  CACHE_KEY,
  DEFAULT_BRANDING,
  fetchBranding,
  normalizeBranding,
  parseCachedBranding,
  readCachedBranding,
  saveCachedBranding,
} from "../../../shared/site-branding.js";

export const useSiteBrandingStore = defineStore("siteBranding", {
  state: () => ({ branding: readCachedBranding(), revision: 0 }),
  actions: {
    apply(value, { persist = true } = {}) {
      const branding = normalizeBranding(value);
      if (!branding) return false;
      this.branding = branding;
      this.revision += 1;
      if (persist) saveCachedBranding(branding);
      applyDocumentBranding(branding);
      return true;
    },
    async refresh() {
      const revision = this.revision;
      try {
        const branding = await fetchBranding();
        // A response started before another tab's admin save must not undo it.
        return revision === this.revision ? this.apply(branding) : false;
      } catch {
        // Offline startup must retain the last known identity or bundled defaults.
        return false;
      }
    },
    initialize() {
      applyDocumentBranding(this.branding);
      const onStorage = (event) => {
        if (event.key !== CACHE_KEY && event.key !== null) return;
        const branding = parseCachedBranding(event.newValue);
        if (branding || event.newValue === null) {
          this.apply(branding || DEFAULT_BRANDING, { persist: false });
        }
      };
      window.addEventListener("storage", onStorage);
      void this.refresh();
      return () => window.removeEventListener("storage", onStorage);
    },
  },
});

if (import.meta.hot) {
  import.meta.hot.accept(
    acceptHMRUpdate(useSiteBrandingStore, import.meta.hot),
  );
}
