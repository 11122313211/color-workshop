/* 应用入口：主题、图标注入、路由注册与启动 */

import { startRouter, register } from "./router.js";
import { icon } from "./icons.js";
import { renderRecipesView } from "./views/recipes.js";
import { renderProcessesView, renderProcessDetail } from "./views/processes.js";
import { renderFilesView } from "./views/files.js";
import { renderMonitorView } from "./views/monitor.js";
import { renderToolsView } from "./views/tools.js";
import { updateNavCount } from "./views/shell.js";

/* ---------- 主题（跟随系统 + 手动切换记忆） ---------- */
function initTheme() {
  const saved = localStorage.getItem("theme");
  const preferDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (preferDark ? "dark" : "light"));

  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme;
      const next = cur === "dark" ? "light" : "dark";
      localStorage.setItem("theme", next);
      applyTheme(next);
    });
  });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const label = theme === "dark" ? "亮色" : "暗色";
  const iconSvg = icon(theme === "dark" ? "sun" : "moon");
  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    const textEl = btn.querySelector("[data-theme-text]");
    if (textEl) textEl.textContent = label;
    const iconEl = btn.querySelector("[data-icon]");
    if (iconEl) iconEl.innerHTML = iconSvg;
  });
  // 通知 canvas 类组件按新主题重绘（observeRedraw 监听）
  window.dispatchEvent(new CustomEvent("themechange", { detail: { theme } }));
}

/* ---------- 全局拖拽兜底：文件拖到 dropzone 之外不得触发浏览器打开 ---------- */
function bindGlobalDragGuard() {
  // 拖拽经过可放置区域时浏览器默认行为会被 dropzone 自身拦截；这里兜底其余区域
  ["dragover", "drop"].forEach((ev) => {
    window.addEventListener(ev, (e) => {
      if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files")) {
        if (!e.defaultPrevented) e.preventDefault();
      }
    });
  });
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
register("#/monitor", renderMonitorView);
register("#/tools", renderToolsView);

initTheme();
injectIcons();
bindNav();
bindGlobalDragGuard();
updateNavCount();
startRouter();
