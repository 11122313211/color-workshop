/* 应用入口：主题、图标注入、路由注册与启动 */

import { startRouter, register } from "./router.js";
import { icon } from "./icons.js";
import { renderRecipesView } from "./views/recipes.js";
import { renderProcessesView, renderProcessDetail } from "./views/processes.js";
import { renderFilesView } from "./views/files.js";
import { renderToolsView } from "./views/tools.js";
import { updateNavCount } from "./views/shell.js";

/* ---------- 主题（跟随系统 + 手动切换记忆） ---------- */
function initTheme() {
  const saved = localStorage.getItem("theme");
  const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (preferDark ? "dark" : "light"));

  document.getElementById("theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    localStorage.setItem("theme", next);
    applyTheme(next);
  });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.getElementById("theme-text").textContent = theme === "dark" ? "亮色" : "暗色";
  const iconEl = document.querySelector("#theme-btn [data-icon]");
  iconEl.innerHTML = icon(theme === "dark" ? "sun" : "moon");
}

/* ---------- 图标注入 ---------- */
function injectIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
}

/* ---------- 导航 ---------- */
function bindNav() {
  document.querySelectorAll(".nav-item[data-route]").forEach((btn) => {
    btn.addEventListener("click", () => {
      location.hash = btn.dataset.route;
    });
  });
}

/* ---------- 路由注册 ---------- */
register("#/recipes", renderRecipesView);
register("#/files", renderFilesView);
register("#/processes", renderProcessesView);
register("#/processes/:id", renderProcessDetail);
register("#/tools", renderToolsView);

initTheme();
injectIcons();
bindNav();
updateNavCount();
startRouter();
