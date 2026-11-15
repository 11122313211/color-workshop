/* hash 路由：#/recipes · #/processes · #/processes/:id · #/files · #/tools */

const routes = [];

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
  const hash = location.hash || "#/recipes";
  const view = document.getElementById("view");
  for (const r of routes) {
    const m = hash.match(r.regex);
    if (m) {
      const params = {};
      r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1])));
      // 侧边栏高亮：取 pattern 的第一段
      setActiveNav("#/" + hash.split("/")[1]);
      window.scrollTo(0, 0);
      view.scrollTop = 0;
      await r.renderFn(view, params);
      return;
    }
  }
  // 兜底 → 配方预测
  navigate("#/recipes");
}

function setActiveNav(hash) {
  document.querySelectorAll(".nav-item[data-route]").forEach((el) => {
    el.classList.toggle("active", el.dataset.route === hash);
  });
}

export function startRouter() {
  window.addEventListener("hashchange", resolve);
  resolve();
}
