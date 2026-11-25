/* hash 路由：#/recipes · #/processes · #/processes/:id · #/files · #/tools */

import { closeAllModals } from "./components.js";

const routes = [];
let navToken = 0;
let activeController = null;
let activeCleanups = [];

function runCleanups() {
  if (activeController) {
    try { activeController.abort(); } catch (_) {}
    activeController = null;
  }
  const fns = activeCleanups;
  activeCleanups = [];
  for (const fn of fns) {
    try { fn(); } catch (_) {}
  }
}

export function register(pattern, renderFn) {
  // pattern 如 "#/processes/:id"
  const keys = [];
  const regex = new RegExp(
    "^" +
      pattern.replace(/:[^/]+/g, (m) => {
        keys.push(m.slice(1));
        return "([^/]+)";
      }) +
      "$"
  );
  routes.push({ regex, keys, renderFn });
}

export function navigate(hash) {
  location.hash = hash;
}

async function resolve() {
  const myToken = ++navToken;
  runCleanups();
  closeAllModals();

  const raw = location.hash || "#/recipes";
  // 拆出 query（如 #/recipes?process=xxx），路径段照常匹配
  const [hash, queryStr = ""] = raw.split("?");
  const query = Object.fromEntries(new URLSearchParams(queryStr));
  const view = document.getElementById("view");
  for (const r of routes) {
    const m = hash.match(r.regex);
    if (m) {
      const params = {};
      try {
        r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
      } catch (_) {
        navigate("#/recipes");
        return;
      }
      const controller = new AbortController();
      activeController = controller;
      const isActive = () => myToken === navToken && !controller.signal.aborted;
      const onCleanup = (fn) => {
        if (typeof fn !== "function") return;
        if (!isActive()) {
          try { fn(); } catch (_) {}
          return;
        }
        activeCleanups.push(fn);
      };
      params.query = query;
      params.signal = controller.signal;
      params.isActive = isActive;
      params.onCleanup = onCleanup;

      // 侧边栏高亮：取 pattern 的第一段
      setActiveNav("#/" + hash.split("/")[1]);
      window.scrollTo(0, 0);
      view.scrollTop = 0;
      const maybeDispose = await r.renderFn(view, params);
      if (typeof maybeDispose === "function") onCleanup(maybeDispose);
      return;
    }
  }
  // 兜底 → 配方预测
  navigate("#/recipes");
}

function setActiveNav(hash) {
  document.querySelectorAll(".nav-item[data-route]").forEach((el) => {
    const active = el.dataset.route === hash;
    el.classList.toggle("active", active);
    if (active) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
}

export function startRouter() {
  window.addEventListener("hashchange", resolve);
  resolve();
}
