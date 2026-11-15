/* 色域视图组件：2D CIE 凸包图 + 伪 3D Lab 体积 + L* 切片 + hover 读数。
 *
 * 渲染器注册表（V0.4 three.js 预留）：注册同名渲染器即可替换渲染层，
 * 视图状态、tooltip、切片滑块、事件层全部复用：
 *   registerRenderer("three", {
 *     mount(stage) -> canvasLike, draw(data, viewState), hitTest(px, py, viewState), dispose()
 *   })
 * DOM 契约恒定：.gamut-stage > canvas + .gamut-tip；.gamut-toolbar / .gamut-controls。
 */

import { drawCie, fetchSpectrum, labToCss, resolveToken, withAlpha, observeRedraw, esc } from "./components.js";

/* ---------- 渲染器注册表 ---------- */
const renderers = new Map();

export function registerRenderer(name, impl) {
  renderers.set(name, impl);
}

/** sRGB 参考原色 xy 色度坐标（与后端 _SRGB_PRIMARIES_XY 一致，唯一真源在组件层） */
export const SRGB_XY = [[0.64, 0.33], [0.30, 0.6], [0.15, 0.06]];

/* ---------- canvas2d 渲染器（2D + 斜投影伪 3D） ---------- */

// 归一化 Lab → 三维坐标：a,b ∈ [-128,128] → [0,1]；L ∈ [0,100] → z ∈ [0,1]
function normalizeLab(lab) {
  return [(lab[1] + 128) / 256, (lab[2] + 128) / 256, lab[0] / 100];
}

/** 斜投影基向量（设计规格 §4.3）：theta 方位角 / phi 倾角（度） */
function basis(thetaDeg, phiDeg) {
  const th = (thetaDeg * Math.PI) / 180;
  const ph = (phiDeg * Math.PI) / 180;
  return {
    right: [Math.cos(th), 0, -Math.sin(th)],
    up: [-Math.sin(ph) * Math.sin(th), Math.cos(ph), -Math.sin(ph) * Math.cos(th)],
    eye: [Math.cos(ph) * Math.sin(th), Math.sin(ph), Math.cos(ph) * Math.cos(th)],
  };
}

function dot3(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

const canvas2d = {
  name: "canvas2d",
  /** 绘制并返回屏幕坐标集（供 hitTest / tooltip） */
  draw(canvas, data, state, spectrum) {
    return state.mode === "3d"
      ? draw3d(canvas, data, state)
      : draw2d(canvas, data, state, spectrum);
  },
  hitTest(screenPts, px, py, radius = 10) {
    let best = null;
    let bestD2 = radius * radius;
    for (const p of screenPts) {
      if (!p) continue;
      const d2 = (p.sx - px) ** 2 + (p.sy - py) ** 2;
      if (d2 <= bestD2) {
        bestD2 = d2;
        best = p;
      }
    }
    return best;
  },
};

function draw2d(canvas, data, state, spectrum) {
  const slice = state.slice;
  const labs = data.lab_points || [];
  // 按 L* 升序绘制：暗色在下、亮色在上，缓解 xy 投影大量重叠时的暗色糊板
  const order = labs.map((_, i) => i).sort((a, b) => labs[a][0] - labs[b][0]);
  const alphaFn =
    slice == null
      ? undefined
      : (i) => (Math.abs(labs[i][0] - slice) <= 6 ? 0.95 : 0.06);
  const projector = drawCie(
    canvas,
    [
      // ColorThink 式嵌套：大色域 sRGB 半透明在下层
      { points: SRGB_XY, color: "var(--accent)", fill: true, fillAlpha: 0.13 },
      {
        points: order.map((i) => data.xy_points[i]),
        labPoints: order.map((i) => labs[i]),
        hull: data.hull,
        color: "var(--indigo)",
        alphaFn,
        pointSize: 1.7,
        hullAlpha: 0.14,
        hullWidth: 2,
      },
    ],
    { spectrum }
  );
  // 屏幕坐标缓存（hover 命中测试用，顺序与绘制顺序一致）
  return order.map((i) => {
    const [sx, sy] = projector.toScreen(data.xy_points[i][0], data.xy_points[i][1]);
    return { sx, sy, lab: labs[i], xy: data.xy_points[i] };
  });
}

function draw3d(canvas, data, state) {
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 420;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, size, size);

  const { right, up, eye } = basis(state.theta, state.phi);
  const MARGIN = 34;
  const project = (v) => ({
    sx: dot3(v, right),
    sy: dot3(v, up),
    depth: dot3(v, eye),
  });

  // —— 点云投影 + 视口归一化（网格与点共用同一 toPx 映射）——
  const labs = data.lab_points || [];
  const slice = state.slice;
  const projected = labs.map((lab) => ({ lab, ...project(normalizeLab(lab)) }));
  if (!projected.length) return [];
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  let dMin = Infinity, dMax = -Infinity;
  for (const p of projected) {
    minX = Math.min(minX, p.sx); maxX = Math.max(maxX, p.sx);
    minY = Math.min(minY, p.sy); maxY = Math.max(maxY, p.sy);
    dMin = Math.min(dMin, p.depth); dMax = Math.max(dMax, p.depth);
  }
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const s = (size - MARGIN * 2) / Math.max(maxX - minX, maxY - minY, 0.001);
  const dSpan = Math.max(dMax - dMin, 0.001);
  const toPx = (p) => ({
    sx: size / 2 + (p.sx - cx) * s,
    sy: size / 2 - (p.sy - cy) * s, // 屏幕坐标 y 向下
    depthN: (p.depth - dMin) / dSpan,
    lab: p.lab,
  });

  // —— 底面 a*b* 网格（L=0 平面，z=0）——
  ctx.strokeStyle = withAlpha(resolveToken("--label-3"), 0.35);
  ctx.lineWidth = 1;
  const gridLine = (v0, v1) => {
    const steps = 4;
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const v = [v0[0] + (v1[0] - v0[0]) * t, v0[1] + (v1[1] - v0[1]) * t, 0];
      const p = toPx(project(v));
      i ? ctx.lineTo(p.sx, p.sy) : ctx.moveTo(p.sx, p.sy);
    }
    ctx.stroke();
  };
  for (let i = 0; i <= 4; i++) {
    const t = i / 4;
    gridLine([t, 0, 0], [t, 1, 0]); // 平行 b 轴
    gridLine([0, t, 0], [1, t, 0]); // 平行 a 轴
  }

  const screenPts = projected.map(toPx);

  // —— painter 算法：由远及近绘制，点尺寸随深度 1.1 → 2.4px ——
  const sorted = [...screenPts].sort((a, b) => a.depthN - b.depthN);
  for (const p of sorted) {
    const inSlice = slice == null || Math.abs(p.lab[0] - slice) <= 6;
    const alpha = slice == null ? 0.9 : inSlice ? 0.95 : 0.07;
    if (alpha <= 0.02) continue;
    ctx.fillStyle = withAlpha(labToCss(p.lab), alpha);
    ctx.beginPath();
    ctx.arc(p.sx, p.sy, 1.1 + p.depthN * 1.3, 0, Math.PI * 2);
    ctx.fill();
  }

  return screenPts;
}

registerRenderer("canvas2d", canvas2d);

/* ---------- 视图组件 ---------- */

/**
 * createGamutView(el, { getData, levels })
 * 返回 { setMode, setSlice, setRotation, onHover, redraw, destroy }
 */
export function createGamutView(el, { getData, levels = 7 } = {}) {
  let mode = "2d";
  let theta = 35;
  let phi = 25;
  let slice = null;
  let hoverCb = null;
  let data = null;
  let spectrum = null;
  let screenPts = [];
  let dragging = false;
  let lastPointer = null;
  let disposed = false;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  el.innerHTML = `
    <div class="gamut-toolbar">
      <div class="seg" data-role="mode">
        <button data-mode="2d" class="active">2D 平面</button>
        <button data-mode="3d">3D 体积</button>
      </div>
      <span class="gamut-hint">3D：拖拽旋转 · 双击复位</span>
    </div>
    <div class="gamut-stage">
      <div class="gamut-loading"><span class="spinner"></span>正在经 ICC 采样色域…</div>
      <canvas class="gamut-canvas" hidden></canvas>
      <div class="gamut-tip" hidden></div>
    </div>
    <div class="gamut-controls">
      <button class="gamut-slice-toggle" role="switch" aria-checked="false" title="按明度切片高亮">
        <span class="chip">L* 切片</span>
      </button>
      <input type="range" class="gamut-slice" min="0" max="100" step="1" value="55" disabled aria-label="L* 切片明度">
      <span class="gamut-slice-val">关</span>
      <span style="flex:1"></span>
      <span class="gamut-sampled"></span>
    </div>
  `;

  const stage = el.querySelector(".gamut-stage");
  const canvas = el.querySelector(".gamut-canvas");
  const tip = el.querySelector(".gamut-tip");
  const loading = el.querySelector(".gamut-loading");
  const sampledEl = el.querySelector(".gamut-sampled");
  const sliceRange = el.querySelector(".gamut-slice");
  const sliceToggle = el.querySelector(".gamut-slice-toggle");
  const sliceVal = el.querySelector(".gamut-slice-val");

  const renderer = renderers.get("canvas2d");

  /* —— 绘制（rAF 节流）—— */
  let rafId = 0;
  function schedule() {
    if (rafId || disposed || !data) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      if (disposed || !data) return;
      screenPts = renderer.draw(canvas, data, { mode, theta, phi, slice }, spectrum);
    });
  }

  /* —— tooltip —— */
  function showTip(p, clientX, clientY) {
    if (!p || !p.lab) { tip.hidden = true; if (hoverCb) hoverCb(null); return; }
    const rect = stage.getBoundingClientRect();
    tip.hidden = false;
    tip.innerHTML = `<b>L* ${p.lab[0].toFixed(1)}</b> · a ${p.lab[1].toFixed(1)} · b ${p.lab[2].toFixed(1)}`;
    const tw = tip.offsetWidth;
    const x = Math.min(Math.max(clientX - rect.left + 14, 6), rect.width - tw - 6);
    const y = Math.max(clientY - rect.top - 34, 6);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
    if (hoverCb) hoverCb(p.lab);
  }

  /* —— 数据加载 —— */
  async function load() {
    loading.hidden = false;
    canvas.hidden = true;
    try {
      [data, spectrum] = await Promise.all([getData(), fetchSpectrum().catch(() => null)]);
      if (disposed) return;
      loading.hidden = true;
      canvas.hidden = false;
      sampledEl.textContent = data.sampled ? `${data.sampled} 采样点 · sRGB 覆盖 ${(data.coverage_srgb * 100).toFixed(0)}%` : "";
      schedule();
    } catch (e) {
      if (disposed) return;
      loading.hidden = true;
      stage.innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
    }
  }

  /* —— 交互 —— */
  canvas.addEventListener("pointermove", (e) => {
    if (mode === "3d" && dragging) {
      const dx = e.clientX - lastPointer.x;
      const dy = e.clientY - lastPointer.y;
      lastPointer = { x: e.clientX, y: e.clientY };
      theta = (theta + dx * 0.5) % 360;
      phi = Math.min(80, Math.max(10, phi + dy * 0.35));
      schedule();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const hit = renderer.hitTest(
      screenPts,
      e.clientX - rect.left,
      e.clientY - rect.top,
      mode === "3d" ? 8 : 10
    );
    showTip(hit, e.clientX, e.clientY);
  });
  canvas.addEventListener("pointerleave", () => {
    tip.hidden = true;
    if (hoverCb) hoverCb(null);
  });
  canvas.addEventListener("pointerdown", (e) => {
    if (mode !== "3d") return;
    dragging = true;
    lastPointer = { x: e.clientX, y: e.clientY };
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add("grabbing");
  });
  canvas.addEventListener("pointerup", (e) => {
    dragging = false;
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
    canvas.classList.remove("grabbing");
  });
  canvas.addEventListener("dblclick", () => {
    if (mode !== "3d") return;
    // prefers-reduced-motion 下直接置位，无动画
    theta = 35; phi = 25;
    schedule();
  });

  el.querySelector('[data-role="mode"]').addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-mode]");
    if (!btn || btn.dataset.mode === mode) return;
    mode = btn.dataset.mode;
    el.querySelectorAll('[data-role="mode"] button').forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    tip.hidden = true;
    schedule();
  });

  sliceToggle.addEventListener("click", () => {
    const on = sliceRange.disabled;
    sliceRange.disabled = !on;
    sliceToggle.setAttribute("aria-checked", String(on));
    sliceToggle.classList.toggle("on", on);
    if (on) {
      slice = +sliceRange.value;
      sliceVal.textContent = `${slice} ±6`;
    } else {
      slice = null;
      sliceVal.textContent = "关";
    }
    schedule();
  });
  sliceRange.addEventListener("input", () => {
    if (sliceRange.disabled) return;
    slice = +sliceRange.value;
    sliceVal.textContent = `${slice} ±6`;
    schedule();
  });

  const disposers = [observeRedraw(canvas, schedule)];

  load();

  return {
    setMode(m) {
      mode = m;
      el.querySelectorAll('[data-role="mode"] button').forEach((b) =>
        b.classList.toggle("active", b.dataset.mode === m)
      );
      schedule();
    },
    setSlice(v) {
      slice = v;
      sliceVal.textContent = v == null ? "关" : `${v} ±6`;
      sliceRange.disabled = v == null;
      schedule();
    },
    setRotation({ theta: t, phi: p }) {
      if (t != null) theta = t;
      if (p != null) phi = p;
      schedule();
    },
    onHover(cb) { hoverCb = cb; },
    redraw: schedule,
    destroy() {
      disposed = true;
      if (rafId) cancelAnimationFrame(rafId);
      disposers.forEach((d) => d());
      el.innerHTML = "";
    },
  };
}
