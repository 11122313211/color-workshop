/* 外壳辅助：侧边栏工艺计数 */

import { apiGet } from "../api.js";

export async function updateNavCount() {
  const el = document.getElementById("nav-process-count");
  if (!el) return;
  try {
    const list = await apiGet("/api/processes");
    if (list.length) {
      el.textContent = list.length;
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  } catch (_) {
    el.hidden = true;
  }
}
