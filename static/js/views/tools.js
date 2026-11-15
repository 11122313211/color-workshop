/* 工具箱视图：CMYK→LAB 正查 · ΔE 对比 · 色域对比 · 色彩转换器 */

import { apiGet, apiPost } from "../api.js";
import { icon } from "../icons.js";
import { toast, swatch, drawCie, labToHex, fetchSpectrum } from "../components.js";

let processes = [];

const TOOLS = [
  ["forward", "CMYK→LAB 正查", "给定墨量配方预测颜色"],
  ["deltae", "ΔE 对比", "两组 Lab 颜色色差检测"],
  ["gamut", "色域对比", "CIE 1931 色度图"],
  ["convert", "色彩转换器", "常见色彩空间互转"],
];

export async function renderToolsView(view) {
  try {
    processes = await apiGet("/api/processes");
  } catch (_) {
    processes = [];
  }

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">工具箱</h1>
        <p class="page-sub">专业色彩工具：正查、色差、色域与转换</p>
      </div>
    </div>
    <div class="seg" id="tool-seg" style="margin-bottom:18px">
      ${TOOLS.map(([k, t]) => `<button data-tool="${k}" class="${k === "forward" ? "active" : ""}">${t}</button>`).join("")}
    </div>
    ${TOOLS.map(([k, t, d], i) => `
      <div class="tool-pane ${k === "forward" ? "active" : ""}" data-pane="${k}">
        <div class="card">
          <div class="card-title">${t}</div>
          <div class="card-desc">${d}</div>
          <div id="tool-${k}"></div>
        </div>
      </div>
    `).join("")}
  `;

  view.querySelectorAll("#tool-seg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      view.querySelectorAll("#tool-seg button").forEach((b) => b.classList.toggle("active", b === btn));
      view.querySelectorAll(".tool-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === btn.dataset.tool));
    });
  });

  buildForward(view.querySelector("#tool-forward"));
  buildDeltaE(view.querySelector("#tool-deltae"));
  buildGamut(view.querySelector("#tool-gamut"));
  buildConvert(view.querySelector("#tool-convert"));
}

/* ---------- CMYK→LAB 正查 ---------- */
function buildForward(box) {
  if (!processes.length) {
    box.innerHTML = emptyState({ iconName: "stack", title: "需要先创建工艺", desc: "正查基于工艺 ICC，先到工艺库创建。" });
    fillEmpty(box, { title: "需要先创建工艺", desc: "正查基于工艺 ICC，先到工艺库创建。" });
    return;
  }
  box.innerHTML = `
    <div class="field-row">
      <div class="field" style="max-width:340px">
        <label>工艺</label>
        <select class="input" id="fq-process">
          ${processes.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.icc_meta.device_space}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="field-row" id="fq-channels" style="margin-top:10px"></div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="fq-go">${icon("wand")}查询</button>
      <div id="fq-result" style="display:flex; align-items:center; gap:10px"></div>
    </div>
  `;

  const renderChannels = () => {
    const p = processes.find((x) => x.id === box.querySelector("#fq-process").value);
    const names = { CMYK: ["C", "M", "Y", "K"], RGB: ["R", "G", "B"], GRAY: ["K"] }[p.icc_meta.device_space] || ["C", "M", "Y", "K"];
    box.querySelector("#fq-channels").innerHTML = names
      .map((n) => `<div class="field" style="max-width:110px"><label>${n} %</label><input class="input fq-v" data-ch="${n}" type="number" min="0" max="100" value="0"></div>`)
      .join("");
  };
  renderChannels();
  box.querySelector("#fq-process").addEventListener("change", renderChannels);

  box.querySelector("#fq-go").addEventListener("click", async () => {
    const values = [...box.querySelectorAll(".fq-v")].map((i) => parseFloat(i.value) || 0);
    try {
      const r = await apiPost("/api/recipes/forward", {
        process_id: box.querySelector("#fq-process").value,
        values,
      });
      box.querySelector("#fq-result").innerHTML = `
        <div class="swatch" style="background:${r.hex}"></div>
        <div class="swatch-meta">Lab(${r.lab.join(", ")})<br>${r.hex}</div>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* ---------- ΔE 对比 ---------- */
function buildDeltaE(box) {
  box.innerHTML = `
    <div class="field-row">
      <div class="field"><label>颜色 1 · L*</label><input class="input de1" id="de-l1" type="number" value="50"></div>
      <div class="field"><label>a*</label><input class="input de1" id="de-a1" type="number" value="20"></div>
      <div class="field"><label>b*</label><input class="input de1" id="de-b1" type="number" value="5"></div>
      <div class="divider-v" style="align-self:center; height:38px"></div>
      <div class="field"><label>颜色 2 · L*</label><input class="input de2" id="de-l2" type="number" value="52"></div>
      <div class="field"><label>a*</label><input class="input de2" id="de-a2" type="number" value="18"></div>
      <div class="field"><label>b*</label><input class="input de2" id="de-b2" type="number" value="7"></div>
      <div class="field"><label>容差</label><input class="input" id="de-tol" type="number" value="2" step="0.1" style="max-width:90px"></div>
    </div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="de-go">${icon("target")}计算 ΔE</button>
      <div id="de-result" style="display:flex;align-items:center;gap:10px"></div>
    </div>
  `;
  box.querySelector("#de-go").addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    try {
      const r = await apiPost("/api/delta-e/lab", {
        lab1: [g("de-l1"), g("de-a1"), g("de-b1")],
        lab2: [g("de-l2"), g("de-a2"), g("de-b2")],
        tolerance: g("de-tol") || 2,
      });
      box.querySelector("#de-result").innerHTML = `
        <span class="badge ${r.passed ? "badge-green" : "badge-red"}">ΔE ${r.delta_e} ${r.passed ? "· 通过" : "· 超差"}</span>
        <span class="swatch" style="background:${labToHex(r.lab1)}"></span>
        <span class="swatch" style="background:${labToHex(r.lab2)}"></span>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* ---------- 色域对比 ---------- */
async function buildGamut(box) {
  box.innerHTML = `
    <div class="field-row" style="margin-bottom:12px">
      <div class="field" style="max-width:340px">
        <label>叠加工艺点集</label>
        <select class="input" id="gm-process">
          <option value="">不叠加</option>
          ${processes.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.icc_meta.device_space}</option>`).join("")}
        </select>
      </div>
    </div>
    <canvas class="cie-canvas" id="gm-cie"></canvas>
    <div class="cie-legend">
      <span><i class="dot" style="background:linear-gradient(90deg,#4400ff,#00ff00,#ff0000)"></i>光谱轨迹</span>
      <span><i class="dot" style="background:#ff6b6b"></i>sRGB</span>
      <span><i class="dot" style="background:#4ecdc4"></i>Display P3</span>
      <span><i class="dot" style="background:#ffd93d"></i>Adobe RGB</span>
      <span><i class="dot" style="background:#8e8e93"></i>工艺采样</span>
    </div>
  `;

  const SPECTRA = [
    { color: "#ff6b6b", spaces: ["srgb"] },
    { color: "#4ecdc4", spaces: ["dci-p3"] },
    { color: "#ffd93d", spaces: ["adobe-rgb"] },
  ];
  const canvas = box.querySelector("#gm-cie");
  let spectrum = null;
  try {
    spectrum = await fetchSpectrum();
  } catch (_) { /* 光谱数据失败仍可画色域 */ }
  const draw = async () => {
    const series = [];
    try {
      const data = await apiGet("/api/visualization/gamut?spaces=srgb,dci-p3,adobe-rgb");
      for (const s of SPECTRA) {
        const poly = s.spaces.flatMap((sp) => data.gamut_polygons[sp] || []);
        if (poly.length) series.push({ points: poly, color: s.color, fill: true });
      }
    } catch (_) { /* 忽略 */ }
    const pid = box.querySelector("#gm-process").value;
    if (pid) {
      try {
        const g = await apiGet(`/api/processes/${pid}/gamut?levels=7`);
        series.push({ points: g.xy_points, color: "#8e8e93" });
      } catch (_) { /* 忽略 */ }
    }
    drawCie(canvas, series, { spectrum });
  };
  canvas.style.maxWidth = "440px";
  draw();
  box.querySelector("#gm-process").addEventListener("change", draw);
}

/* ---------- 色彩转换器 ---------- */
function buildConvert(box) {
  box.innerHTML = `
    <div class="field-row">
      <div class="field"><label>R</label><input class="input cv" id="cv-r" type="number" min="0" max="255" value="180"></div>
      <div class="field"><label>G</label><input class="input cv" id="cv-g" type="number" min="0" max="255" value="130"></div>
      <div class="field"><label>B</label><input class="input cv" id="cv-b" type="number" min="0" max="255" value="85"></div>
      <div class="field"><label>源空间</label>
        <select class="input" id="cv-from">
          <option value="srgb">sRGB</option><option value="adobe-rgb">Adobe RGB</option>
          <option value="dci-p3">Display P3</option><option value="rec2020">Rec.2020</option>
          <option value="prophoto">ProPhoto</option>
        </select>
      </div>
      <div class="field"><label>目标空间</label>
        <select class="input" id="cv-to">
          <option value="lab">CIELAB</option><option value="xyz">CIEXYZ</option>
          <option value="srgb">sRGB</option><option value="dci-p3">Display P3</option>
          <option value="adobe-rgb">Adobe RGB</option>
        </select>
      </div>
    </div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="cv-go">${icon("swap")}转换</button>
      <div id="cv-result" style="display:flex;align-items:center;gap:10px"></div>
    </div>
  `;
  box.querySelector("#cv-go").addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    try {
      const r = await apiPost("/api/convert", {
        rgb: [g("cv-r"), g("cv-g"), g("cv-b")],
        from_space: box.querySelector("#cv-from").value,
        to_space: box.querySelector("#cv-to").value,
      });
      const isLab = box.querySelector("#cv-to").value === "lab";
      box.querySelector("#cv-result").innerHTML = `
        ${isLab ? `<div class="swatch" style="background:${labToHex(r.result)}"></div>` : ""}
        <span class="swatch-meta">${box.querySelector("#cv-to").value.toUpperCase()}(${r.result.map((v) => Math.round(v * 1000) / 1000).join(", ")})</span>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
