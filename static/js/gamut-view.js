/* 色域视图组件：2D CIE 凸包图 + 3D 体积 + L* 切片 + hover 读数。
 *
 * 渲染器注册表：同名渲染器可替换渲染层，视图状态、tooltip、切片滑块、事件层全部复用：
 *   - 'canvas2d'：2D CIE 图 + 斜投影伪 3D（V0.3，零依赖，始终可用）
 *   - 'three'：  WebGL 真 3D（V0.4，gamut-three.js 首次切 3D 时动态 import，
 *                失败/无 WebGL 自动回退 canvas2d 伪 3D 并在角标标注）
 * DOM 契约：.gamut-stage > canvas.gamut-canvas(2D) + canvas.gamut-canvas-gl(3D, 惰性创建)
 *           + .gamut-tip；.gamut-toolbar / .gamut-controls。
 * 两类 canvas 不能共存于同一元素（2D/WebGL 上下文互斥），故 3D 用独立元素按 hidden 切换。
 */

import { drawCie, fetchSpectrum, labToCss, resolveToken, withAlpha, observeRedraw, esc } from "./components.js";

/* ---------- 渲染器注册表 ---------- */
const renderers = new Map();

export function registerRenderer(name, impl) {
  renderers.set(name, impl);
}

/** 标准 D65 参考色域元数据（与后端 _REFERENCE_GAMUTS_XY 一致） */
export const STANDARD_GAMUTS = [
  {
    id: "srgb",
    label: "sRGB",
    shortLabel: "sRGB",
    color: "var(--accent)",
    dotClass: "gamut-dot-srgb",
    points: [[0.64, 0.33], [0.30, 0.60], [0.15, 0.06]],
  },
  {
    id: "dci-p3",
    label: "Display P3",
    shortLabel: "P3",
    color: "var(--teal)",
    dotClass: "gamut-dot-p3",
    points: [[0.68, 0.32], [0.265, 0.69], [0.15, 0.06]],
  },
  {
    id: "adobe-rgb",
    label: "Adobe RGB",
    shortLabel: "Adobe RGB",
    color: "var(--yellow)",
    dotClass: "gamut-dot-adobe",
    points: [[0.64, 0.33], [0.21, 0.71], [0.15, 0.06]],
  },
  {
    id: "rec2020",
    label: "Rec.2020",
    shortLabel: "Rec.2020",
    color: "var(--orange)",
    dotClass: "gamut-dot-rec2020",
    points: [[0.708, 0.292], [0.17, 0.797], [0.131, 0.046]],
  },
];

/** sRGB 参考原色 xy 色度坐标（向后兼容旧引用） */
export const SRGB_XY = STANDARD_GAMUTS[0].points;

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
    if (state.mode === "3d") {
      return { points: draw3d(canvas, data, state), spectrumPoints: [] };
    }
    return draw2d(canvas, data, state, spectrum);
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
  // 构建原索引 -> 排序后索引的反向映射，确保凸包顶点与 L* 切片严格对齐排序后数组
  const rank = new Int32Array(labs.length);
  order.forEach((origIdx, sortedIdx) => {
    rank[origIdx] = sortedIdx;
  });
  const orderedPoints = order.map((i) => data.xy_points[i]);
  const orderedLabs = order.map((i) => labs[i]);
  const orderedHull =
    state.showHull !== false && Array.isArray(data.hull) && data.hull.length >= 3
      ? data.hull.map((origIdx) => rank[origIdx]).filter((i) => i !== undefined)
      : null;

  const alphaFn =
    slice == null
      ? undefined
      : (i) => (Math.abs(orderedLabs[i][0] - slice) <= 6 ? 0.95 : 0.06);
  const hiddenPoints = state.showPoints === false;

  // ColorThink 式嵌套：大参考色域在下层、小参考色域在上层，用户可任意勾选/叠加
  const selectedGamuts = state.selectedGamuts || new Set(["srgb"]);
  const series = [];
  for (let i = STANDARD_GAMUTS.length - 1; i >= 0; i--) {
    const g = STANDARD_GAMUTS[i];
    if (!selectedGamuts.has(g.id)) continue;
    series.push({
      points: g.points,
      color: g.color,
      fill: true,
      fillAlpha: g.id === "srgb" ? 0.13 : 0.08,
    });
  }
  series.push({
    points: orderedPoints,
    labPoints: orderedLabs,
    hull: orderedHull,
    color: "var(--indigo)",
    alphaFn,
    pointSize: 1.7,
    pointAlpha: 0.72,
    hidePoints: hiddenPoints,
    hullAlpha: 0.14,
    hullWidth: 2,
  });

  const projector = drawCie(canvas, series, {
    spectrum: state.showSpectrum === false ? null : spectrum,
  });
  // 屏幕坐标缓存（hover 命中测试用，顺序与绘制顺序一致）
  const points = orderedPoints.map((xy, i) => {
    const [sx, sy] = projector.toScreen(xy[0], xy[1]);
    return { sx, sy, lab: orderedLabs[i], xy };
  });
  return { points, spectrumPoints: projector.spectrumPoints || [] };
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
  if (state.showPoints !== false) for (const p of sorted) {
    const inSlice = slice == null || Math.abs(p.lab[0] - slice) <= 6;
    const alpha = slice == null ? 0.9 : inSlice ? 0.95 : 0.07;
    if (alpha <= 0.02) continue;
    ctx.fillStyle = withAlpha(labToCss(p.lab), alpha);
    ctx.beginPath();
    ctx.arc(p.sx, p.sy, 1.1 + p.depthN * 1.3, 0, Math.PI * 2);
    ctx.fill();
  }

  return state.showPoints === false ? [] : screenPts;
}

registerRenderer("canvas2d", canvas2d);

/* ---------- 视图组件 ---------- */

/**
 * createGamutView(el, { getData, levels, intents, defaultIntent, onData })
 * 返回 { setMode, setSlice, setRotation, onHover, redraw, destroy }
 */
export function createGamutView(
  el,
  { getData, levels = 7, intents = false, defaultIntent = "relative", onData = null } = {}
) {
  let mode = "2d";
  let intent = defaultIntent;
  let theta = 35;
  let phi = 25;
  let zoom = 2.2;
  let slice = null;
  let hoverCb = null;
  let data = null;
  let spectrum = null;
  let screenPts = [];
  let spectrumPts = [];
  let showSpectrum = true;
  let showHull = true;
  let showPoints = true;
  const selectedGamuts = new Set(["srgb"]);
  const dataCache = new Map();
  let loadSeq = 0;
  let dragging = false;
  let lastPointer = null;
  let disposed = false;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const refGamutButtons = STANDARD_GAMUTS.map(
    (g) => `
        <button class="gamut-layer-toggle" type="button" data-gamut="${g.id}"
          aria-pressed="${selectedGamuts.has(g.id) ? "true" : "false"}"
          title="显示 / 隐藏 ${g.label} 参考色域（仅 2D）">
          <i class="gamut-dot ${g.dotClass}" aria-hidden="true"></i><span>${g.label}</span>
        </button>`
  ).join("");

  const intentSwitchHtml = intents
    ? `
      <div class="seg seg-compact" data-role="intent" title="切换 ICC 色度采样意图">
        <button type="button" data-intent="relative" class="${intent === "relative" ? "active" : ""}"
          title="相对色度（A2B1 表，纸白归一化到 D65）">相对色度</button>
        <button type="button" data-intent="absolute" class="${intent === "absolute" ? "active" : ""}"
          title="绝对色度（含真实纸张底色偏移）">绝对色度</button>
      </div>`
    : "";

  el.innerHTML = `
    <div class="gamut-toolbar">
      <div class="seg" data-role="mode">
        <button data-mode="2d" class="active">2D 平面</button>
        <button data-mode="3d">3D 体积</button>
      </div>
      ${intentSwitchHtml}
      <div class="gamut-legend" role="group" aria-label="色域与图层选择">
        <button class="gamut-layer-toggle" type="button" data-layer="spectrum" aria-pressed="true"
          title="显示 / 隐藏 CIE 光谱轨迹（仅 2D）">
          <i class="gamut-dot gamut-dot-spectrum" aria-hidden="true"></i><span>光谱轨迹</span>
        </button>
        ${refGamutButtons}
        <button class="gamut-layer-toggle" type="button" data-layer="hull" aria-pressed="true"
          title="显示 / 隐藏工艺凸包边界（3D 下控制凸包外壳）">
          <i class="gamut-dot gamut-dot-hull" aria-hidden="true"></i><span>工艺凸包</span>
        </button>
        <button class="gamut-layer-toggle" type="button" data-layer="points" aria-pressed="true"
          title="显示 / 隐藏采样点（按 L* 着色）">
          <i class="gamut-dot gamut-dot-proc" aria-hidden="true"></i><span>采样点</span>
        </button>
      </div>
      <span class="gamut-hint" data-role="hint">悬停读取：采样点 / 光谱波长</span>
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
      <span class="gamut-render-badge chip" hidden></span>
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

  /* —— 渲染器选择：2D 恒为 canvas2d；3D 优先 three（WebGL），失败回退 canvas2d —— */
  let threeState = "none"; // none | loading | ready | failed
  let glCanvas = null;

  function webglAvailable() {
    try {
      const probe = document.createElement("canvas");
      return !!(
        window.WebGLRenderingContext &&
        (probe.getContext("webgl2") || probe.getContext("webgl"))
      );
    } catch (_) {
      return false;
    }
  }

  async function ensureThree() {
    if (threeState !== "none") return;
    threeState = "loading";
    try {
      await import("./gamut-three.js"); // 模块顶层自注册 'three'
      threeState = renderers.has("three") && webglAvailable() ? "ready" : "failed";
    } catch (_) {
      threeState = "failed";
    }
    updateBadge();
    schedule();
  }

  function updateBadge() {
    stage.classList.toggle("mode-3d", mode === "3d");
    updateLayerUI();
    const b = el.querySelector(".gamut-render-badge");
    if (!b) return;
    if (mode !== "3d") {
      b.hidden = true;
      return;
    }
    b.hidden = false;
    b.textContent = threeState === "ready" ? "WebGL · three.js" : "Canvas 回退";
  }

  function updateSampledText() {
    if (!data || !data.sampled) {
      sampledEl.textContent = "";
      return;
    }
    const covMap = data.coverages || { srgb: data.coverage_srgb };
    const picked = STANDARD_GAMUTS.filter((g) => selectedGamuts.has(g.id));
    const parts = [`${data.sampled} 采样点`];
    if (picked.length === 1) {
      const g = picked[0];
      const v = covMap[g.id];
      if (v != null) parts.push(`${g.label} 覆盖 ${(v * 100).toFixed(0)}%`);
    } else if (picked.length > 1) {
      for (const g of picked) {
        const v = covMap[g.id];
        if (v != null) parts.push(`${g.shortLabel} ${(v * 100).toFixed(0)}%`);
      }
    } else if (data.coverage_srgb != null) {
      parts.push(`sRGB 覆盖 ${(data.coverage_srgb * 100).toFixed(0)}%`);
    }
    sampledEl.textContent = parts.join(" · ");
  }

  function updateLayerUI() {
    const spectrumToggle = el.querySelector('[data-layer="spectrum"]');
    const hullToggle = el.querySelector('[data-layer="hull"]');
    const pointsToggle = el.querySelector('[data-layer="points"]');
    const hint = el.querySelector('[data-role="hint"]');
    [
      [spectrumToggle, showSpectrum],
      [hullToggle, showHull],
      [pointsToggle, showPoints],
    ].forEach(([btn, on]) => {
      if (!btn) return;
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("is-off", !on);
    });
    el.querySelectorAll("button[data-gamut]").forEach((btn) => {
      const id = btn.dataset.gamut;
      const on = selectedGamuts.has(id);
      const meta = STANDARD_GAMUTS.find((g) => g.id === id);
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("is-off", !on);
      btn.disabled = mode === "3d";
      btn.title =
        mode === "3d"
          ? "标准参考色域仅在 2D 平面显示"
          : `显示 / 隐藏 ${meta?.label || id} 参考色域`;
    });
    if (spectrumToggle) {
      spectrumToggle.disabled = mode === "3d";
      spectrumToggle.title =
        mode === "3d" ? "光谱轨迹仅在 2D 平面显示" : "显示 / 隐藏 CIE 光谱轨迹";
    }
    if (hullToggle) {
      hullToggle.title =
        mode === "3d" ? "显示 / 隐藏 3D 凸包外壳网格" : "显示 / 隐藏工艺凸包轮廓";
    }
    if (hint) {
      hint.textContent =
        mode === "3d" ? "拖拽旋转 · 双击复位" : "悬停读取：采样点 / 光谱波长";
    }
    updateSampledText();
  }
  updateLayerUI();

  /* —— 绘制（rAF 节流；按模式选 canvas 与渲染器）—— */
  let rafId = 0;
  function applyDrawResult(result) {
    if (Array.isArray(result)) {
      screenPts = result;
      spectrumPts = [];
    } else {
      screenPts = result?.points || [];
      spectrumPts = result?.spectrumPoints || [];
    }
  }

  function schedule() {
    if (rafId || disposed || !data) return;
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      if (disposed || !data) return;
      const renderState = {
        mode,
        theta,
        phi,
        slice,
        zoom,
        showSpectrum,
        showHull,
        showPoints,
        selectedGamuts,
      };
      if (mode === "3d" && threeState === "ready") {
        if (!glCanvas) {
          glCanvas = document.createElement("canvas");
          glCanvas.className = "gamut-canvas gamut-canvas-gl";
          glCanvas.setAttribute("aria-hidden", "true");
          stage.insertBefore(glCanvas, tip);
        }
        canvas.hidden = true;
        glCanvas.hidden = false;
        let result = renderers
          .get("three")
          .draw(glCanvas, data, renderState, spectrum);
        if (result === null) {
          // WebGL 上下文创建失败 → 回退伪 3D
          threeState = "failed";
          updateBadge();
          glCanvas.hidden = true;
          canvas.hidden = false;
          result = renderers
            .get("canvas2d")
            .draw(canvas, data, renderState, spectrum);
        }
        applyDrawResult(result);
      } else {
        if (glCanvas) glCanvas.hidden = true;
        canvas.hidden = false;
        const result = renderers
          .get("canvas2d")
          .draw(canvas, data, renderState, spectrum);
        applyDrawResult(result);
      }
    });
  }

  /* —— tooltip —— */
  function hideTip() {
    tip.hidden = true;
    if (hoverCb) hoverCb(null);
  }

  function showTip(p, clientX, clientY) {
    if (!p) {
      hideTip();
      return;
    }
    if (p.kind === "spectrum") {
      tip.hidden = false;
      tip.innerHTML =
        `<b>CIE 1931 · ${esc(p.wavelength)} nm</b>` +
        `<span class="gamut-tip-sub">x ${p.x.toFixed(4)} · y ${p.y.toFixed(4)}</span>`;
      if (hoverCb) hoverCb(null);
    } else if (p.lab) {
      tip.hidden = false;
      tip.innerHTML = `<b>L* ${p.lab[0].toFixed(1)}</b> · a ${p.lab[1].toFixed(1)} · b ${p.lab[2].toFixed(1)}`;
      if (hoverCb) hoverCb(p.lab);
    } else {
      hideTip();
      return;
    }

    const rect = stage.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    const pointerX = clientX - rect.left;
    const pointerY = clientY - rect.top;
    let x = pointerX + 14;
    if (x + tw > rect.width - 6) x = pointerX - tw - 14;
    let y = pointerY - th - 12;
    if (y < 6) y = pointerY + 16;
    x = Math.min(Math.max(x, 6), Math.max(6, rect.width - tw - 6));
    y = Math.min(Math.max(y, 6), Math.max(6, rect.height - th - 6));
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  /* —— 数据加载 —— */
  async function load(targetIntent = intent) {
    const seq = ++loadSeq;
    const cached = dataCache.get(targetIntent);
    if (cached && spectrum !== undefined) {
      data = cached;
      loading.hidden = true;
      updateSampledText();
      if (onData) onData(data);
      schedule();
      return;
    }
    loading.hidden = false;
    if (!data) canvas.hidden = true;
    try {
      const [loadedData, loadedSpectrum] = await Promise.all([
        getData(targetIntent),
        spectrum ? Promise.resolve(spectrum) : fetchSpectrum().catch(() => null),
      ]);
      if (disposed || seq !== loadSeq) return;
      data = loadedData;
      spectrum = loadedSpectrum;
      dataCache.set(targetIntent, data);
      loading.hidden = true;
      if (mode !== "3d" || threeState !== "ready") canvas.hidden = false;
      updateSampledText();
      if (onData) onData(data);
      schedule();
    } catch (e) {
      if (disposed || seq !== loadSeq) return;
      loading.hidden = true;
      stage.innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
    }
  }

  /* —— 交互（绑定在 stage 上，2D/3D 两个 canvas 共用同一套手感）—— */
  const activeCanvas = () =>
    mode === "3d" && threeState === "ready" && glCanvas && !glCanvas.hidden
      ? glCanvas
      : canvas;

  let hoverRafId = 0;
  let pendingHover = null;

  stage.addEventListener("pointermove", (e) => {
    if (mode === "3d" && dragging) {
      const dx = e.clientX - lastPointer.x;
      const dy = e.clientY - lastPointer.y;
      lastPointer = { x: e.clientX, y: e.clientY };
      theta = (theta + dx * 0.5) % 360;
      phi = Math.min(80, Math.max(10, phi + dy * 0.35));
      schedule();
      return;
    }
    pendingHover = { clientX: e.clientX, clientY: e.clientY };
    if (hoverRafId) return;
    hoverRafId = requestAnimationFrame(() => {
      hoverRafId = 0;
      if (disposed || !pendingHover) return;
      const { clientX, clientY } = pendingHover;
      pendingHover = null;
      const rect = activeCanvas().getBoundingClientRect();
      const px = clientX - rect.left;
      const py = clientY - rect.top;
      const hitTest = renderers.get("canvas2d").hitTest;
      let hit = showPoints ? hitTest(screenPts, px, py, mode === "3d" ? 8 : 10) : null;
      if (mode === "2d" && showSpectrum) {
        const spectralHit = hitTest(spectrumPts, px, py, 7);
        if (spectralHit) {
          const sampleDistance = hit ? Math.hypot(hit.sx - px, hit.sy - py) : Infinity;
          const spectrumDistance = Math.hypot(spectralHit.sx - px, spectralHit.sy - py);
          // 轨迹与点云重叠时稍微偏向轨迹，保证细线也能被选中。
          if (!hit || spectrumDistance <= sampleDistance + 2.5) hit = spectralHit;
        }
      }
      showTip(hit, clientX, clientY);
    });
  });
  stage.addEventListener("pointerleave", hideTip);
  stage.addEventListener("pointerdown", (e) => {
    if (mode !== "3d") return;
    hideTip();
    dragging = true;
    lastPointer = { x: e.clientX, y: e.clientY };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("grabbing");
  });
  stage.addEventListener("pointerup", (e) => {
    dragging = false;
    try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
    stage.classList.remove("grabbing");
  });
  stage.addEventListener("wheel", (e) => {
    if (mode !== "3d" || threeState !== "ready") return;
    e.preventDefault();
    zoom = Math.min(4, Math.max(1.4, zoom * (e.deltaY > 0 ? 1.08 : 0.92)));
    schedule();
  }, { passive: false });
  stage.addEventListener("dblclick", () => {
    if (mode !== "3d") return;
    // prefers-reduced-motion 下直接置位，无动画
    theta = 35; phi = 25; zoom = 2.2;
    schedule();
  });

  el.querySelector('[data-role="mode"]').addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-mode]");
    if (!btn || btn.dataset.mode === mode) return;
    mode = btn.dataset.mode;
    el.querySelectorAll('[data-role="mode"] button').forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    hideTip();
    if (mode === "3d") ensureThree();
    updateBadge();
    schedule();
  });

  const intentBar = el.querySelector('[data-role="intent"]');
  if (intentBar) {
    intentBar.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-intent]");
      if (!btn || btn.dataset.intent === intent) return;
      intent = btn.dataset.intent;
      intentBar.querySelectorAll("button[data-intent]").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      hideTip();
      load(intent);
    });
  }

  el.querySelector(".gamut-legend").addEventListener("click", (e) => {
    const gamutBtn = e.target.closest("button[data-gamut]");
    if (gamutBtn && !gamutBtn.disabled) {
      const id = gamutBtn.dataset.gamut;
      if (selectedGamuts.has(id)) selectedGamuts.delete(id);
      else selectedGamuts.add(id);
      hideTip();
      updateLayerUI();
      schedule();
      return;
    }
    const btn = e.target.closest("button[data-layer]");
    if (!btn || btn.disabled) return;
    if (btn.dataset.layer === "spectrum") {
      showSpectrum = !showSpectrum;
    } else if (btn.dataset.layer === "hull") {
      showHull = !showHull;
    } else if (btn.dataset.layer === "points") {
      showPoints = !showPoints;
    }
    hideTip();
    updateLayerUI();
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

  // 监听 stage 而非 2D canvas，确保 3D 模式下 canvas.hidden=true 时窗口缩放也能重绘
  const disposers = [observeRedraw(stage, schedule)];

  load();

  return {
    setMode(m) {
      mode = m;
      el.querySelectorAll('[data-role="mode"] button').forEach((b) =>
        b.classList.toggle("active", b.dataset.mode === m)
      );
      if (mode === "3d") ensureThree();
      updateBadge();
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
      if (hoverRafId) cancelAnimationFrame(hoverRafId);
      disposers.forEach((d) => d());
      const threeR = renderers.get("three");
      if (threeR && threeR.dispose) threeR.dispose();
      el.innerHTML = "";
    },
  };
}
