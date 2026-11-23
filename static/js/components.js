/* 通用组件：toast / 空状态 / 徽标 / swatch / sparkline / 折叠披露 */

import { icon } from "./icons.js";
import { apiGet } from "./api.js";

/* ---------- toast ---------- */
export function toast(msg, type = "ok", { action } = {}) {
  let host = document.querySelector(".toast-host");
  if (!host) {
    host = document.createElement("div");
    host.className = "toast-host";
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  const ic = type === "ok" ? "check" : type === "warn" ? "warn" : "xmark";
  if (type === "err") host.setAttribute("aria-live", "assertive");
  el.innerHTML = `${icon(ic)}<span></span>`;
  el.querySelector("span").textContent = msg;
  if (action && action.label) {
    const btn = document.createElement("button");
    btn.className = "toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", () => { dismiss(); action.onClick && action.onClick(); });
    el.appendChild(btn);
  }
  host.appendChild(el);
  const ttl = type === "err" ? 4000 : 2600;
  const timer = setTimeout(dismiss, ttl);
  function dismiss() {
    clearTimeout(timer);
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }
  return dismiss;
}

/* ---------- 空状态 / 错误态 / 骨架 ---------- */
export function emptyState({ iconName = "flask", title = "", desc = "", action = "" }) {
  return `<div class="empty">${icon(iconName)}
    <div class="empty-title">${esc(title)}</div>
    <div class="empty-desc">${esc(desc)}</div>${action}</div>`;
}

/** 错误态（可重试）：返回 HTML，调用方绑定 .btn-retry 点击 */
export function errorState(msg, { retry = true } = {}) {
  return `<div class="card"><div class="issue issue-error">${icon("xmark")}<span>${esc(msg)}</span>
    ${retry ? `<span style="flex:1"></span><button class="btn btn-secondary btn-sm btn-retry">重试</button>` : ""}</div></div>`;
}

/** 骨架屏卡片（列表首屏占位，reduced-motion 下为静态灰块） */
export function skeletonCards(n = 4) {
  return `<div class="card-grid">${Array.from(
    { length: n },
    () => `<div class="card skeleton-card" aria-hidden="true">
      <div class="skeleton sk-icon"></div>
      <div class="skeleton sk-line"></div>
      <div class="skeleton sk-line short"></div>
    </div>`
  ).join("")}</div>`;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 徽标 ---------- */
export function statusBadge(deltaE, tolerance = 2.0) {
  if (deltaE <= tolerance) return `<span class="badge badge-green">${icon("check")}达标 ΔE ${deltaE}</span>`;
  if (deltaE <= 5) return `<span class="badge badge-orange">可用 ΔE ${deltaE}</span>`;
  return `<span class="badge badge-red">超差 ΔE ${deltaE}</span>`;
}

/* ---------- Lab → sRGB（D50 PCS，Bradford 色适应） ---------- */
// Bradford D50->D65 矩阵
const M_ADAPT = [
  [0.9555766, -0.0230393, 0.0631636],
  [-0.0282895, 1.0099416, 0.0210077],
  [0.0122982, -0.0204830, 1.3299098],
];
const XYZ_D50 = [0.96422, 1.0, 0.82521]; // D50 白点 XYZ
const XYZ_D65 = [0.95047, 1.0, 1.08883]; // D65 白点 XYZ
const M_XYZ2RGB = [
  [3.2404542, -1.5371385, -0.4985314],
  [-0.9692660, 1.8760108, 0.0415560],
  [0.0556434, -0.2040259, 1.0572252],
];

function mul3(m, v) {
  return [
    m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
    m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
    m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
  ];
}

/** CIELAB（D50 白点，ICC PCS）→ sRGB hex（用于前端色块显示） */
export function labToHex(lab) {
  const [L, a, b] = lab;
  const fy = (L + 16) / 116;
  const fx = fy + a / 500;
  const fz = fy - b / 200;
  const f2inv = (f) => (f ** 3 > 0.008856 ? f ** 3 : (f - 4 / 29) / 7.787);
  const xyzD50 = [
    f2inv(fx) * XYZ_D50[0],
    (L > 8 ? ((L + 16) / 116) ** 3 : L / (116 * 7.787)) * XYZ_D50[1],
    f2inv(fz) * XYZ_D50[2],
  ];
  // Von Kries（Bradford 变换域）D50 → D65
  const rel = mul3(M_ADAPT, xyzD50);
  const xyzD65 = rel.map((v, i) => (v * XYZ_D65[i]) / XYZ_D50[i]);
  let rgb = mul3(M_XYZ2RGB, xyzD65).map((c) => {
    c = c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
    return Math.round(Math.min(1, Math.max(0, c)) * 255);
  });
  return "#" + rgb.map((v) => v.toString(16).padStart(2, "0")).join("");
}

/** CIELAB（D50 PCS）→ linear sRGB [0,1]^3（WebGL 顶点色用，不经过 gamma） */
export function labToLinearRgb(lab) {
  const [L, a, b] = lab;
  const fy = (L + 16) / 116;
  const fx = fy + a / 500;
  const fz = fy - b / 200;
  const f2inv = (f) => (f ** 3 > 0.008856 ? f ** 3 : (f - 4 / 29) / 7.787);
  const xyzD50 = [
    f2inv(fx) * XYZ_D50[0],
    (L > 8 ? ((L + 16) / 116) ** 3 : L / (116 * 7.787)) * XYZ_D50[1],
    f2inv(fz) * XYZ_D50[2],
  ];
  const rel = mul3(M_ADAPT, xyzD50);
  const xyzD65 = rel.map((v, i) => (v * XYZ_D65[i]) / XYZ_D50[i]);
  return mul3(M_XYZ2RGB, xyzD65).map((c) => Math.min(1, Math.max(0, c)));
}

export function swatch(lab, { size = "", label = "" } = {}) {
  const hex = labToHex(lab);
  return `<div class="swatch ${size}" style="background:${labToCss(lab)}" title="Lab(${lab.map((v) => Math.round(v * 10) / 10).join(", ")})">
    </div>${label ? `<div class="swatch-label">${label}<br><span class="swatch-meta">${hex}</span></div>` : ""}`;
}

/* ---------- canvas 颜色工具（主题令牌 / L* 真彩 / display-p3） ---------- */

/** 读取 CSS 设计令牌的真实计算值（canvas 不认识 var()，必须解析成具体颜色） */
export function resolveToken(name) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || "#8e8e93";
}

let _p3Css = null;
/** 浏览器是否支持 color(display-p3 …) 颜色语法 */
export function p3Available() {
  if (_p3Css === null) {
    _p3Css = typeof CSS !== "undefined" && CSS.supports && CSS.supports("color", "color(display-p3 1 0 0)");
  }
  return _p3Css;
}

let _p3Ctx = null;
/** canvas 2d context 的最优色域选项（支持则用 display-p3，色域边缘色样不再被裁剪） */
export function bestCtxOptions() {
  if (_p3Ctx !== null) return _p3Ctx;
  _p3Ctx = { colorSpace: "srgb" };
  try {
    const c = document.createElement("canvas").getContext("2d", { colorSpace: "display-p3" });
    if (c && c.getContextAttributes && c.getContextAttributes().colorSpace === "display-p3") {
      _p3Ctx = { colorSpace: "display-p3" };
    }
  } catch (_) { /* 老浏览器回退 srgb */ }
  return _p3Ctx;
}

// XYZ(D65) → linear Display-P3（经 colour-science Display P3 colourspace 矩阵校准）
const M_XYZ2P3 = [
  [2.4934969, -0.9313836, -0.4027108],
  [-0.8294890, 1.7626641, 0.0236247],
  [0.0358458, -0.0761724, 0.9568845],
];

/** CIELAB（D50 PCS）→ CSS 颜色串。P3 可用时输出 color(display-p3 …)，否则 sRGB hex */
export function labToCss(lab) {
  if (!p3Available()) return labToHex(lab);
  const [L, a, b] = lab;
  const fy = (L + 16) / 116;
  const fx = fy + a / 500;
  const fz = fy - b / 200;
  const f2inv = (f) => (f ** 3 > 0.008856 ? f ** 3 : (f - 4 / 29) / 7.787);
  const xyzD50 = [
    f2inv(fx) * XYZ_D50[0],
    (L > 8 ? ((L + 16) / 116) ** 3 : L / (116 * 7.787)) * XYZ_D50[1],
    f2inv(fz) * XYZ_D50[2],
  ];
  const rel = mul3(M_ADAPT, xyzD50);
  const xyzD65 = rel.map((v, i) => (v * XYZ_D65[i]) / XYZ_D50[i]);
  const rgb = mul3(M_XYZ2P3, xyzD65).map((c) => Math.min(1, Math.max(0, c)));
  return `color(display-p3 ${rgb.map((v) => v.toFixed(4)).join(" ")})`;
}

/** 把颜色串转为带 alpha 的 canvas 可用颜色（hex / var() / rgb / p3 均可） */
export function withAlpha(color, alpha) {
  let c = String(color);
  if (c.startsWith("var(")) c = resolveToken(c.slice(4, -1).trim());
  c = c.trim();
  if (c.startsWith("#")) {
    const h = c.length === 4
      ? "#" + [...c.slice(1)].map((ch) => ch + ch).join("")
      : c;
    const r = parseInt(h.slice(1, 3), 16), g = parseInt(h.slice(3, 5), 16), b = parseInt(h.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }
  if (c.startsWith("rgb(")) return c.replace("rgb(", "rgba(").replace(")", `,${alpha})`);
  if (c.startsWith("rgba(")) return c.replace(/[\d.]+\)$/, `${alpha})`);
  if (c.startsWith("color(")) return c.replace(/\)$/, ` / ${alpha})`);
  return c; // 其他语法原样返回
}

/* ---------- 配方芯片（C/M/Y/K 大数字 + 条） ---------- */
const CHANNEL_COLORS = { C: "#30b0c7", M: "#af52de", Y: "#ffcc00", K: "#48484a", R: "#ff3b30", G: "#34c759", B: "#007aff" };

export function recipeLine(recipe) {
  return `<div class="recipe-line">${Object.entries(recipe)
    .map(([ch, val]) => {
      const color = CHANNEL_COLORS[ch] || "var(--accent)";
      return `<div class="recipe-chip">
        <span class="val">${val}</span>
        <span class="ch" style="color:${color}">${ch}</span>
        <span class="bar"><i style="width:${val}%;background:${color}"></i></span>
      </div>`;
    })
    .join("")}</div>`;
}

/* ---------- 迭代轨迹 sparkline ---------- */
export function sparkline(values, { width = 120, height = 30, tolerance = 2.0 } = {}) {
  if (!values.length) return "";
  const max = Math.max(...values, tolerance * 1.4);
  const pts = values.map((v, i) => {
    const x = (i / Math.max(values.length - 1, 1)) * (width - 4) + 2;
    const y = height - 3 - (v / max) * (height - 6);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const tolY = height - 3 - (tolerance / max) * (height - 6);
  const color = values[values.length - 1] <= tolerance ? "var(--green)" : "var(--orange)";
  return `<svg width="${width}" height="${height}" class="spark" aria-hidden="true">
    <line x1="2" y1="${tolY}" x2="${width - 2}" y2="${tolY}" stroke="var(--label-3)" stroke-dasharray="3 3" stroke-width="1"/>
    <polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    ${pts.map((p) => `<circle cx="${p.split(",")[0]}" cy="${p.split(",")[1]}" r="2" fill="${color}"/>`).join("")}
  </svg>`;
}

/* ---------- 折叠披露 ---------- */
export function disclosure(title, bodyHtml, open = false) {
  return `<div class="disclosure ${open ? "open" : ""}">
    <button class="disclosure-head" type="button">${icon("chevronR")}<span>${title}</span></button>
    <div class="disclosure-body">${bodyHtml}</div>
  </div>`;
}
export function bindDisclosures(root) {
  root.querySelectorAll(".disclosure-head").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest(".disclosure").classList.toggle("open"));
  });
}

/* ---------- CIE 1931 色度图（canvas，DPR 校准，等比坐标） ---------- */

// 光谱轨迹数据缓存（Promise 复用，多视图共享）
let _spectrumPromise = null;

/** 拉取 CIE 1931 光谱轨迹（后端 colour CMF 权威计算），模块级缓存 */
export function fetchSpectrum() {
  if (!_spectrumPromise) {
    _spectrumPromise = apiGet("/api/visualization/spectrum").catch((e) => {
      _spectrumPromise = null; // 失败允许重试
      throw e;
    });
  }
  return _spectrumPromise;
}

const LOCUS_LABEL_WAVELENGTHS = [460, 500, 540, 580, 620, 700];

function rectsOverlap(a, b) {
  return !(a.right < b.left || a.left > b.right || a.bottom < b.top || a.top > b.bottom);
}

/**
 * 光谱轨迹置顶绘制：先铺背景描边，再叠加波长标注并做碰撞避让。
 * 返回所有轨迹点的屏幕坐标，供 gamut-view 的悬停命中使用。
 */
function drawSpectrumOverlay(ctx, spectrum, px, py, size, pad) {
  if (!spectrum || !spectrum.locus || spectrum.locus.length < 2) return [];
  const locus = spectrum.locus;
  const spectrumPoints = locus.map((point) => ({
    kind: "spectrum",
    wavelength: point.wavelength,
    x: point.x,
    y: point.y,
    sx: px(point.x),
    sy: py(point.y),
    hex: point.hex,
  }));
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  const trace = () => {
    ctx.beginPath();
    locus.forEach((p, i) => {
      const x = px(p.x);
      const y = py(p.y);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
  };

  // 中性底描边把光谱轨迹从密集点云中“托”出来，再按波长着色。
  trace();
  ctx.strokeStyle = withAlpha(resolveToken("--bg-elevated"), 0.92);
  ctx.lineWidth = 6;
  ctx.stroke();
  ctx.lineWidth = 2.4;
  for (let i = 1; i < locus.length; i++) {
    ctx.beginPath();
    ctx.strokeStyle = locus[i].hex;
    ctx.moveTo(px(locus[i - 1].x), py(locus[i - 1].y));
    ctx.lineTo(px(locus[i].x), py(locus[i].y));
    ctx.stroke();
  }

  if (spectrum.purple_line) {
    const [a, b] = spectrum.purple_line;
    ctx.beginPath();
    ctx.strokeStyle = withAlpha(resolveToken("--bg-elevated"), 0.92);
    ctx.lineWidth = 5;
    ctx.setLineDash([5, 4]);
    ctx.moveTo(px(a[0]), py(a[1]));
    ctx.lineTo(px(b[0]), py(b[1]));
    ctx.stroke();
    ctx.strokeStyle = "#c95cd6";
    ctx.lineWidth = 1.8;
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // 波长标注：沿“白点 → 轨迹点”的外法线放置，并用屏幕矩形避让。
  const centerX = px(0.3127);
  const centerY = py(0.329);
  const placed = [];
  ctx.font = "600 9px ui-monospace, monospace";
  ctx.textBaseline = "middle";
  for (const wavelength of LOCUS_LABEL_WAVELENGTHS) {
    const index = locus.findIndex((p) => p.wavelength === wavelength);
    if (index < 0) continue;
    const point = locus[index];
    const x = px(point.x);
    const y = py(point.y);
    let nx = x - centerX;
    let ny = y - centerY;
    const norm = Math.hypot(nx, ny) || 1;
    nx /= norm;
    ny /= norm;
    const text = String(wavelength);
    const textWidth = ctx.measureText(text).width;
    let selected = null;
    for (const distance of [10, 15, 21]) {
      const align = nx < -0.25 ? "right" : nx > 0.25 ? "left" : "center";
      let anchorX = x + nx * distance;
      const anchorY = y + ny * distance;
      let left =
        align === "right" ? anchorX - textWidth : align === "left" ? anchorX : anchorX - textWidth / 2;
      let top = anchorY - 5;
      if (left < pad + 2) {
        anchorX += pad + 2 - left;
        left = pad + 2;
      } else if (left + textWidth > size - pad - 2) {
        anchorX -= left + textWidth - (size - pad - 2);
        left = size - pad - 2 - textWidth;
      }
      if (top < 2) {
        top = 2;
      } else if (top + 10 > size - 2) {
        top = size - 12;
      }
      const rect = {
        left: left - 2,
        right: left + textWidth + 2,
        top,
        bottom: top + 10,
      };
      if (!placed.some((other) => rectsOverlap(rect, other))) {
        selected = { align, anchorX, anchorY, rect };
        break;
      }
    }
    if (!selected) continue;
    placed.push(selected.rect);
    ctx.beginPath();
    ctx.strokeStyle = withAlpha(resolveToken("--label-3"), 0.55);
    ctx.lineWidth = 1;
    ctx.moveTo(x + nx * 4, y + ny * 4);
    ctx.lineTo(selected.anchorX, selected.anchorY);
    ctx.stroke();
    ctx.textAlign = selected.align;
    ctx.strokeStyle = withAlpha(resolveToken("--bg-elevated"), 0.96);
    ctx.lineWidth = 3.2;
    ctx.strokeText(text, selected.anchorX, selected.anchorY);
    ctx.fillStyle = withAlpha(resolveToken("--label-2"), 0.92);
    ctx.fillText(text, selected.anchorX, selected.anchorY);
  }

  // 白点标注与各图层共用同一套描边，避免在点云上失去可读性。
  const wx = px(0.3127);
  const wy = py(0.329);
  ctx.beginPath();
  ctx.fillStyle = withAlpha(resolveToken("--bg-elevated"), 0.96);
  ctx.arc(wx, wy, 5.2, 0, Math.PI * 2);
  ctx.fill();
  ctx.beginPath();
  ctx.fillStyle = withAlpha(resolveToken("--label-2"), 0.95);
  ctx.arc(wx, wy, 2.6, 0, Math.PI * 2);
  ctx.fill();
  ctx.textAlign = "left";
  ctx.font = "600 9px ui-monospace, monospace";
  ctx.strokeStyle = withAlpha(resolveToken("--bg-elevated"), 0.96);
  ctx.lineWidth = 3.2;
  ctx.strokeText("D65", wx + 7, wy - 8);
  ctx.fillStyle = withAlpha(resolveToken("--label-2"), 0.9);
  ctx.fillText("D65", wx + 7, wy - 8);
  ctx.restore();
  return spectrumPoints;
}

/**
 * 绘制 CIE 1931 xy 色度图。
 * series: [{points: [[x,y]...], color, fill, hull, labPoints, alphaFn, pointSize}]
 *   - color 支持 hex / "var(--token)"（自动解析主题令牌真值）
 *   - hull: 凸包顶点在 points 中的索引（闭合轮廓，工艺色域边界）
 *   - labPoints: 与 points 等长对齐的 Lab 数组 → 采样点按 L* 真彩着色
 *   - alphaFn: (i) => alpha，用于 L* 切片淡出等逐点透明度
 * opts.spectrum: {locus: [{wavelength,x,y,hex}], purple_line: [[x,y],[x,y]]}
 *                来自 fetchSpectrum()；不传则只画网格与系列。
 *
 * 返回 {size, scale, toScreen(x,y)}：供调用方做 hover 最近邻命中测试。
 * 坐标系要点：xy 色度图必须等比（x/y 同一像素比例），否则马蹄形失真。
 */
export function drawCie(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 400;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext("2d", bestCtxOptions());
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, size, size);

  const PAD = 14;
  const X_MAX = 0.78;
  const Y_MAX = 0.87;
  // 统一比例尺：像素/色度单位，x 与 y 相同
  const scale = (size - PAD * 2) / Y_MAX;
  const px = (x) => PAD + x * scale;
  const py = (y) => size - PAD - y * scale;

  // 网格 + 坐标刻度（颜色随主题令牌，切换主题重绘后自动跟随）
  ctx.strokeStyle = withAlpha(resolveToken("--label-3"), 0.18);
  ctx.fillStyle = withAlpha(resolveToken("--label-2"), 0.8);
  ctx.font = "10px -apple-system, sans-serif";
  ctx.lineWidth = 1;
  for (let t = 0.1; t < 0.86; t += 0.1) {
    const v = Math.round(t * 10) / 10;
    ctx.beginPath();
    ctx.moveTo(px(v), py(0));
    ctx.lineTo(px(v), py(Y_MAX));
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(px(0), py(v));
    ctx.lineTo(px(X_MAX), py(v));
    ctx.stroke();
    ctx.fillText(v.toFixed(1), px(v) - 8, size - 4);
    ctx.fillText(v.toFixed(1), 4, py(v) + 3);
  }

  // 白点 D65
  ctx.fillStyle = withAlpha(resolveToken("--label-2"), 0.9);
  ctx.beginPath();
  ctx.arc(px(0.3127), py(0.329), 3, 0, Math.PI * 2);
  ctx.fill();

  // 系列（色域多边形 / 凸包 / 点集）
  for (const s of series) {
    if (!s.points || !s.points.length) continue;
    const solid = withAlpha(s.color, 1); // 解析 var() 令牌为真实颜色（canvas 不识别 CSS 变量）
    // 凸包轮廓（工艺色域边界，绘制在点云之下）
    if (s.hull && s.hull.length >= 3) {
      ctx.beginPath();
      s.hull.forEach((idx, i) => {
        const [x, y] = s.points[idx];
        i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y));
      });
      ctx.closePath();
      ctx.fillStyle = withAlpha(s.color, s.hullAlpha ?? 0.12);
      ctx.fill();
      ctx.strokeStyle = solid;
      ctx.lineWidth = s.hullWidth ?? 2;
      ctx.stroke();
    }
    if (s.fill && s.points.length > 2) {
      ctx.beginPath();
      s.points.forEach(([x, y], i) => (i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))));
      ctx.closePath();
      ctx.fillStyle = withAlpha(s.color, s.fillAlpha ?? 0.13);
      ctx.fill();
      ctx.strokeStyle = solid;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    } else if (!s.hidePoints && s.labPoints && s.labPoints.length === s.points.length) {
      // L* 真彩点云（数据即色）：支持逐点 alpha（L* 切片淡出）
      const r = s.pointSize ?? 1.8;
      for (let i = 0; i < s.points.length; i++) {
        const alpha = s.alphaFn ? s.alphaFn(i) : (s.pointAlpha ?? 0.85);
        if (alpha <= 0.01) continue;
        const [x, y] = s.points[i];
        ctx.fillStyle = withAlpha(labToCss(s.labPoints[i]), alpha);
        ctx.beginPath();
        ctx.arc(px(x), py(y), r, 0, Math.PI * 2);
        ctx.fill();
      }
    } else if (!s.hidePoints) {
      ctx.fillStyle = withAlpha(s.color, s.dotAlpha ?? 0.4);
      for (const [x, y] of s.points) {
        ctx.beginPath();
        ctx.arc(px(x), py(y), s.pointSize ?? 1.6, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  // 凸包轮廓最后再描一遍，避免被高密度采样点盖住边界。
  for (const s of series) {
    if (!s.hull || s.hull.length < 3) continue;
    ctx.beginPath();
    s.hull.forEach((idx, i) => {
      const [x, y] = s.points[idx];
      i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y));
    });
    ctx.closePath();
    ctx.strokeStyle = withAlpha(s.color, 1);
    ctx.lineWidth = s.hullWidth ?? 2;
    ctx.stroke();
  }

  // 光谱轨迹最后绘制，确保不会被填充色域或点云遮住；标注带背景描边与碰撞避让。
  const spectrumPoints = drawSpectrumOverlay(ctx, opts.spectrum, px, py, size, PAD);

  return { size, scale, spectrumPoints, toScreen: (x, y) => [px(x), py(y)] };
}

/* ---------- 模态（Esc / 焦点圈闭 / 焦点还原 / aria） ---------- */
export function openModal(html, { onClose } = {}) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(mask);

  const modal = mask.querySelector(".modal");
  const prevFocus = document.activeElement;
  let closed = false;

  const focusables = () =>
    modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');

  requestAnimationFrame(() => {
    const f = focusables();
    if (f.length) f[0].focus();
  });

  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    mask.remove();
    document.body.style.removeProperty("overflow");
    if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus();
    if (onClose) onClose();
  }

  function onKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      // 焦点圈闭：Tab 循环限制在模态内
      const f = [...focusables()].filter((el) => !el.disabled && el.offsetParent !== null);
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  document.addEventListener("keydown", onKey, true);
  document.body.style.setProperty("overflow", "hidden");
  mask.addEventListener("click", (e) => {
    if (e.target === mask) close();
  });

  return { mask, modal, close };
}

/** iOS 风格危险确认弹层（替代原生 confirm）。resolve(true)=确认 / false=取消 */
export function confirmDanger({ title, message = "", confirmText = "确认" }) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const m = openModal(
      `
      <div class="confirm-danger">
        <div class="cd-icon">${icon("warn")}</div>
        <div class="cd-title">${esc(title)}</div>
        ${message ? `<div class="cd-msg">${esc(message)}</div>` : ""}
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:18px">
          <button class="btn btn-danger btn-block" data-cd="ok">${esc(confirmText)}</button>
          <button class="btn btn-ghost btn-block" data-cd="cancel">取消</button>
        </div>
      </div>
      `,
      { onClose: () => done(false) }
    );
    m.modal.classList.add("modal-confirm");
    m.modal.querySelector('[data-cd="ok"]').addEventListener("click", () => { done(true); m.close(); });
    m.modal.querySelector('[data-cd="cancel"]').addEventListener("click", () => m.close());
  });
}

/* ---------- canvas 重绘契约：resize + 主题切换自动重绘 ---------- */
export function observeRedraw(target, redrawFn) {
  const ro = new ResizeObserver(() => redrawFn());
  ro.observe(target);
  const onTheme = () => redrawFn();
  window.addEventListener("themechange", onTheme);
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onMq = () => redrawFn();
  mq.addEventListener("change", onMq);
  return () => {
    ro.disconnect();
    window.removeEventListener("themechange", onTheme);
    mq.removeEventListener("change", onMq);
  };
}
