/* 通用组件：toast / 空状态 / 徽标 / swatch / sparkline / 折叠披露 */

import { icon } from "./icons.js";
import { apiGet } from "./api.js";

/* ---------- toast ---------- */
export function toast(msg, type = "ok") {
  let host = document.querySelector(".toast-host");
  if (!host) {
    host = document.createElement("div");
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  const ic = type === "ok" ? "check" : type === "warn" ? "warn" : "xmark";
  el.innerHTML = `${icon(ic)}<span></span>`;
  el.querySelector("span").textContent = msg;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

/* ---------- 空状态 ---------- */
export function emptyState({ iconName = "flask", title = "", desc = "", action = "" }) {
  return `<div class="empty">${icon(iconName)}
    <div class="empty-title">${esc(title)}</div>
    <div class="empty-desc">${esc(desc)}</div>${action}</div>`;
}

function esc(s) {
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

export function swatch(lab, { size = "", label = "" } = {}) {
  const hex = labToHex(lab);
  return `<div class="swatch ${size}" style="background:${hex}" title="Lab(${lab.map((v) => Math.round(v * 10) / 10).join(", ")})">
    </div>${label ? `<div class="swatch-label">${label}<br><span class="swatch-meta">${hex}</span></div>` : ""}`;
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

/**
 * 绘制 CIE 1931 xy 色度图。
 * series: [{points: [[x,y]...], color, fill}] —— 色域多边形或散点集
 * opts.spectrum: {locus: [{wavelength,x,y,hex}], purple_line: [[x,y],[x,y]]}
 *                来自 fetchSpectrum()；不传则只画网格与系列。
 *
 * 坐标系要点：xy 色度图必须等比（x/y 同一像素比例），否则马蹄形失真。
 * 以 y 值域 0.87 为基准计算统一比例尺，x（0.78）自然占宽度的 78/87。
 */
export function drawCie(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 400;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, size, size);

  const PAD = 14;
  const X_MAX = 0.78;
  const Y_MAX = 0.87;
  // 统一比例尺：像素/色度单位，x 与 y 相同
  const scale = (size - PAD * 2) / Y_MAX;
  const px = (x) => PAD + x * scale;
  const py = (y) => size - PAD - y * scale;

  // 网格 + 坐标刻度
  ctx.strokeStyle = "rgba(128,128,128,0.14)";
  ctx.fillStyle = "rgba(128,128,128,0.45)";
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
  ctx.fillStyle = "rgba(128,128,128,0.9)";
  ctx.beginPath();
  ctx.arc(px(0.3127), py(0.329), 3, 0, Math.PI * 2);
  ctx.fill();

  // 光谱轨迹（逐段光谱色）+ 紫线闭合 + 波长标注
  const spectrum = opts.spectrum;
  if (spectrum && spectrum.locus && spectrum.locus.length > 1) {
    const locus = spectrum.locus;
    ctx.lineWidth = 2.6;
    ctx.lineCap = "round";
    for (let i = 1; i < locus.length; i++) {
      ctx.beginPath();
      ctx.strokeStyle = locus[i].hex;
      ctx.moveTo(px(locus[i - 1].x), py(locus[i - 1].y));
      ctx.lineTo(px(locus[i].x), py(locus[i].y));
      ctx.stroke();
    }
    // 紫线：380nm → 700nm 直线闭合
    if (spectrum.purple_line) {
      const [a, b] = spectrum.purple_line;
      ctx.beginPath();
      ctx.strokeStyle = "#c95cd6";
      ctx.lineWidth = 2;
      ctx.setLineDash([5, 4]);
      ctx.moveTo(px(a[0]), py(a[1]));
      ctx.lineTo(px(b[0]), py(b[1]));
      ctx.stroke();
      ctx.setLineDash([]);
    }
    // 关键波长标注（置于轨迹外侧）
    ctx.fillStyle = "rgba(128,128,128,0.75)";
    ctx.font = "9px ui-monospace, monospace";
    for (const wl of [460, 500, 540, 580, 620, 660, 700]) {
      const p = locus.find((q) => q.wavelength === wl);
      if (!p) continue;
      const x = px(p.x);
      const y = py(p.y);
      // 依位置决定标注偏移方向（大致朝远离轨迹的一侧）
      let dx = 0, dy = 0, align = "center";
      if (wl <= 470) { dx = -4; align = "right"; }
      else if (wl <= 510) { dy = -6; }
      else if (wl <= 560) { dx = -2; dy = -6; align = "right"; }
      else if (wl <= 620) { dx = 2; dy = -6; }
      else { dx = 6; dy = 3; align = "left"; }
      ctx.textAlign = align;
      ctx.fillText(String(wl), x + dx, y + dy);
    }
    ctx.textAlign = "left";
  }

  // 系列（色域多边形/点集）
  for (const s of series) {
    if (!s.points || !s.points.length) continue;
    if (s.fill && s.points.length > 2) {
      ctx.beginPath();
      s.points.forEach(([x, y], i) => (i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))));
      ctx.closePath();
      ctx.fillStyle = s.color + "22";
      ctx.fill();
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.6;
      ctx.stroke();
    } else {
      ctx.fillStyle = s.color + "66";
      for (const [x, y] of s.points) {
        ctx.beginPath();
        ctx.arc(px(x), py(y), 1.6, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
}

/* ---------- 模态 ---------- */
export function openModal(html) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `<div class="modal">${html}</div>`;
  mask.addEventListener("click", (e) => {
    if (e.target === mask) mask.remove();
  });
  document.body.appendChild(mask);
  return { mask, modal: mask.querySelector(".modal"), close: () => mask.remove() };
}
