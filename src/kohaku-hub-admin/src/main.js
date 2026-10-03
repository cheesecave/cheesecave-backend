import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import { routes } from "vue-router/auto-routes";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "element-plus/theme-chalk/dark/css-vars.css";
import "uno.css";
import "./styles/colors.css";
import "./style.css";
import App from "./App.vue";
import { useSiteBrandingStore } from "./stores/siteBranding";

const app = createApp(App);
const pinia = createPinia();

const router = createRouter({
  history: createWebHistory("/admin/"),
  routes,
});

app.use(pinia);
app.use(router);
app.use(ElementPlus);

const siteBrandingStore = useSiteBrandingStore(pinia);
void siteBrandingStore.refresh();

app.mount("#app");
