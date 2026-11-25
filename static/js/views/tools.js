/* 工具箱视图：CMYK→LAB 正查 · ΔE 对比 · 色域对比 · 色彩转换器 · 显示器档案 */

import { apiGet, apiPost, apiDelete, apiPostRaw, downloadDataUri } from "../api.js";
import { icon } from "../icons.js";
import {
  toast, drawCie, labToHex, fetchSpectrum, emptyState, errorState, observeRedraw,
  openModal, confirmDanger, esc, safeHex,
} from "../components.js";
import { STANDARD_GAMUTS } from "../gamut-view.js";

let processes = [];
let displays = [];

const TOOLS = [
  ["forward", "CMYK→LAB 正查", "给定墨量配方预测颜色"],
  ["deltae", "ΔE 对比", "两组 Lab 颜色色差检测"],
  ["gamut", "色域对比", "CIE 1931 色度图"],
  ["convert", "色彩转换器", "常见色彩空间互转"],
  ["display", "显示器", "校准档案与软打样门禁"],
  ["integrity", "数据维护", "完整性扫描、Dry Run 与修复"],
];

export async function renderToolsView(view, params = {}) {
  const signal = params.signal;
  const isActive = params.isActive || (() => view.isConnected);
  const onCleanup = params.onCleanup || (() => {});

  try {
    const [procs, disps] = await Promise.all([
      apiGet("/api/processes", { signal }),
      apiGet("/api/displays", { signal }).catch(() => []),
    ]);
    if (!isActive()) return;
    processes = procs;
    displays = disps;
  } catch (e) {
    if (e.name === "AbortError" || !isActive()) return;
    view.innerHTML = `
      <div class="page-head">
        <div>
          <h1 class="large-title">工具箱</h1>
          <p class="page-sub">专业色彩工具：正查、色差、色域、转换与数据维护</p>
        </div>
      </div>
      ${errorState(e.message)}`;
    view.querySelector(".btn-retry")?.addEventListener("click", () => renderToolsView(view, params));
    return;
  }

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">工具箱</h1>
        <p class="page-sub">专业色彩工具：正查、色差、色域、转换与数据维护</p>
      </div>
    </div>
    <div class="seg tool-seg" id="tool-seg" style="margin-bottom:18px">
      ${TOOLS.map(([k, t]) => `<button data-tool="${k}" class="${k === "forward" ? "active" : ""}">${t}</button>`).join("")}
    </div>
    ${TOOLS.map(([k, t, d]) => `
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

  const ctx = { signal, isActive, onCleanup };
  buildForward(view.querySelector("#tool-forward"));
  buildDeltaE(view.querySelector("#tool-deltae"));
  buildGamut(view.querySelector("#tool-gamut"), ctx);
  buildConvert(view.querySelector("#tool-convert"));
  buildDisplay(view.querySelector("#tool-display"), ctx);
  buildIntegrity(view.querySelector("#tool-integrity"), ctx);
}

/* ---------- CMYK→LAB 正查 ---------- */
function buildForward(box) {
  if (!processes.length) {
    box.innerHTML = emptyState({ iconName: "stack", title: "需要先创建工艺", desc: "正查基于工艺 ICC，先到工艺库创建。" });
    return;
  }
  box.innerHTML = `
    <div class="field-row">
      <div class="field" style="max-width:340px">
        <label>工艺</label>
        <select class="input" id="fq-process">
          ${processes.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.icc_meta?.device_space || "")}</option>`).join("")}
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
    const names = { CMYK: ["C", "M", "Y", "K"], RGB: ["R", "G", "B"], GRAY: ["K"] }[p?.icc_meta?.device_space] || ["C", "M", "Y", "K"];
    box.querySelector("#fq-channels").innerHTML = names
      .map((n) => `<div class="field" style="max-width:110px"><label>${n} %</label><input class="input fq-v" data-ch="${n}" type="number" min="0" max="100" value="0"></div>`)
      .join("");
  };
  renderChannels();
  box.querySelector("#fq-process").addEventListener("change", renderChannels);

  const fqBtn = box.querySelector("#fq-go");
  fqBtn.addEventListener("click", async () => {
    const values = [...box.querySelectorAll(".fq-v")].map((i) => {
      const v = parseFloat(i.value);
      return Number.isFinite(v) ? v : 0;
    });
    fqBtn.disabled = true;
    try {
      const r = await apiPost("/api/recipes/forward", {
        process_id: box.querySelector("#fq-process").value,
        values,
      });
      const hex = safeHex(r.hex);
      box.querySelector("#fq-result").innerHTML = `
        <div class="swatch" style="background:${hex}"></div>
        <div class="swatch-meta">Lab(${r.lab.map((v) => Number(v).toFixed(2)).join(", ")})<br>${esc(r.hex)}</div>`;
    } catch (e) {
      toast(e.message, "err");
    } finally {
      fqBtn.disabled = false;
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
  const deBtn = box.querySelector("#de-go");
  deBtn.addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    const lab1 = [g("de-l1"), g("de-a1"), g("de-b1")];
    const lab2 = [g("de-l2"), g("de-a2"), g("de-b2")];
    if (![...lab1, ...lab2].every(Number.isFinite)) {
      return toast("请输入有效的 Lab 数值", "warn");
    }
    const tol = g("de-tol");
    deBtn.disabled = true;
    try {
      const r = await apiPost("/api/delta-e/lab", {
        lab1,
        lab2,
        tolerance: Number.isFinite(tol) && tol > 0 ? tol : 2,
      });
      box.querySelector("#de-result").innerHTML = `
        <span class="badge ${r.passed ? "badge-green" : "badge-red"}">ΔE ${esc(r.delta_e)} ${r.passed ? "· 通过" : "· 超差"}</span>
        <span class="swatch" style="background:${labToHex(r.lab1)}"></span>
        <span class="swatch" style="background:${labToHex(r.lab2)}"></span>`;
    } catch (e) {
      toast(e.message, "err");
    } finally {
      deBtn.disabled = false;
    }
  });
}

/* ---------- 色域对比 ---------- */
async function buildGamut(box, ctx) {
  const selectedGamuts = new Set(["srgb", "dci-p3", "adobe-rgb"]);
  let showSpectrum = true;
  let showHull = true;
  let showPoints = true;

  const gamutButtons = STANDARD_GAMUTS.map(
    (g) => `
      <button class="gamut-layer-toggle" type="button" data-gamut="${g.id}"
        aria-pressed="${selectedGamuts.has(g.id) ? "true" : "false"}">
        <i class="gamut-dot ${g.dotClass}" aria-hidden="true"></i><span>${g.label}</span>
      </button>`
  ).join("");

  box.innerHTML = `
    <div class="field-row" style="margin-bottom:12px">
      <div class="field" style="max-width:340px">
        <label>叠加工艺点集</label>
        <select class="input" id="gm-process">
          <option value="">不叠加</option>
          ${processes.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.icc_meta?.device_space || "")}</option>`).join("")}
        </select>
      </div>
    </div>
    <canvas class="cie-canvas" id="gm-cie"></canvas>
    <div class="gamut-legend" id="gm-legend" role="group" aria-label="色域与图层选择" style="margin-top:10px">
      <button class="gamut-layer-toggle" type="button" data-layer="spectrum" aria-pressed="true">
        <i class="gamut-dot gamut-dot-spectrum" aria-hidden="true"></i><span>光谱轨迹</span>
      </button>
      ${gamutButtons}
      <button class="gamut-layer-toggle" type="button" data-layer="hull" aria-pressed="true">
        <i class="gamut-dot gamut-dot-hull" aria-hidden="true"></i><span>工艺凸包</span>
      </button>
      <button class="gamut-layer-toggle" type="button" data-layer="points" aria-pressed="true">
        <i class="gamut-dot gamut-dot-proc" aria-hidden="true"></i><span>采样点</span>
      </button>
    </div>
  `;

  const canvas = box.querySelector("#gm-cie");
  canvas.style.maxWidth = "440px";

  // 缓存几何数据与工艺色域，resize/主题切换仅重绘画布，不触发网络请求
  let spectrum = null;
  const polyBySpace = new Map(STANDARD_GAMUTS.map((g) => [g.id, g.points]));
  const procGamutCache = new Map();
  let activeProcData = null;
  let gamutReqSeq = 0;

  const updateLegendUI = () => {
    const legend = box.querySelector("#gm-legend");
    if (!legend) return;
    legend.querySelectorAll("button[data-gamut]").forEach((btn) => {
      const on = selectedGamuts.has(btn.dataset.gamut);
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("is-off", !on);
    });
    const layers = { spectrum: showSpectrum, hull: showHull, points: showPoints };
    legend.querySelectorAll("button[data-layer]").forEach((btn) => {
      const on = !!layers[btn.dataset.layer];
      btn.setAttribute("aria-pressed", String(on));
      btn.classList.toggle("is-off", !on);
      if (btn.dataset.layer === "hull" || btn.dataset.layer === "points") {
        btn.disabled = !activeProcData;
      }
    });
  };

  const redraw = () => {
    const series = [];
    for (let i = STANDARD_GAMUTS.length - 1; i >= 0; i--) {
      const g = STANDARD_GAMUTS[i];
      if (!selectedGamuts.has(g.id)) continue;
      const pts = polyBySpace.get(g.id) || g.points;
      if (pts.length) {
        series.push({ points: pts, color: g.color, fill: true, fillAlpha: g.id === "srgb" ? 0.13 : 0.08 });
      }
    }
    if (activeProcData) {
      series.push({
        points: activeProcData.xy_points,
        labPoints: activeProcData.lab_points,
        hull: showHull ? activeProcData.hull : null,
        hidePoints: !showPoints,
        color: "var(--indigo)",
        pointSize: 1.8,
      });
    }
    drawCie(canvas, series, { spectrum: showSpectrum ? spectrum : null });
  };

  updateLegendUI();

  box.querySelector("#gm-legend").addEventListener("click", (e) => {
    const gBtn = e.target.closest("button[data-gamut]");
    if (gBtn) {
      const id = gBtn.dataset.gamut;
      if (selectedGamuts.has(id)) selectedGamuts.delete(id);
      else selectedGamuts.add(id);
      updateLegendUI();
      redraw();
      return;
    }
    const lBtn = e.target.closest("button[data-layer]");
    if (!lBtn || lBtn.disabled) return;
    if (lBtn.dataset.layer === "spectrum") showSpectrum = !showSpectrum;
    else if (lBtn.dataset.layer === "hull") showHull = !showHull;
    else if (lBtn.dataset.layer === "points") showPoints = !showPoints;
    updateLegendUI();
    redraw();
  });

  // 一次性注册观察器，并在路由离开时销毁
  const disposeRedraw = observeRedraw(canvas, redraw);
  ctx.onCleanup(disposeRedraw);

  // 初始加载光谱轨迹 + 标准色域多边形（只发一次请求）
  const [specRes, gamutRes] = await Promise.all([
    fetchSpectrum().catch(() => null),
    apiGet("/api/visualization/gamut?spaces=srgb,dci-p3,adobe-rgb,rec2020", { signal: ctx.signal }).catch(() => null),
  ]);
  if (!ctx.isActive()) return;
  spectrum = specRes;
  if (gamutRes?.gamut_polygons) {
    for (const [sp, poly] of Object.entries(gamutRes.gamut_polygons)) {
      if (Array.isArray(poly) && poly.length >= 3) polyBySpace.set(sp, poly);
    }
  }
  redraw();

  const loadProcessGamut = async () => {
    const seq = ++gamutReqSeq;
    const pid = box.querySelector("#gm-process").value;
    if (!pid) {
      activeProcData = null;
      updateLegendUI();
      redraw();
      return;
    }
    if (procGamutCache.has(pid)) {
      activeProcData = procGamutCache.get(pid);
      updateLegendUI();
      redraw();
      return;
    }
    try {
      const g = await apiGet(`/api/processes/${encodeURIComponent(pid)}/gamut?levels=7`, { signal: ctx.signal });
      if (!ctx.isActive() || seq !== gamutReqSeq) return;
      procGamutCache.set(pid, g);
      activeProcData = g;
      updateLegendUI();
      redraw();
    } catch (e) {
      if (e.name !== "AbortError" && ctx.isActive() && seq === gamutReqSeq) {
        toast(`工艺色域加载失败：${e.message}`, "err");
      }
    }
  };
  box.querySelector("#gm-process").addEventListener("change", loadProcessGamut);
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
  const cvBtn = box.querySelector("#cv-go");
  cvBtn.addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    const rgb = [g("cv-r"), g("cv-g"), g("cv-b")];
    if (!rgb.every(Number.isFinite)) {
      return toast("请输入有效的 RGB 数值", "warn");
    }
    cvBtn.disabled = true;
    try {
      const toSpace = box.querySelector("#cv-to").value;
      const r = await apiPost("/api/convert", {
        rgb,
        from_space: box.querySelector("#cv-from").value,
        to_space: toSpace,
      });
      const isLab = toSpace === "lab";
      box.querySelector("#cv-result").innerHTML = `
        ${isLab ? `<div class="swatch" style="background:${labToHex(r.result)}"></div>` : ""}
        <span class="swatch-meta">${esc(toSpace.toUpperCase())}(${r.result.map((v) => Math.round(v * 1000) / 1000).join(", ")})</span>`;
    } catch (e) {
      toast(e.message, "err");
    } finally {
      cvBtn.disabled = false;
    }
  });
}

/* ---------- 数据维护：完整性扫描 / Dry Run / 应用修复 ---------- */
function readPath(obj, path) {
  return path.split(".").reduce((cur, key) => (cur == null ? null : cur[key]), obj);
}

function firstReportValue(report, paths) {
  for (const path of paths) {
    const value = readPath(report, path);
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

function integrityCount(value, preferred = []) {
  if (value == null) return 0;
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object") {
    for (const key of preferred) {
      if (typeof value[key] === "number") return value[key];
    }
    for (const key of ["count", "total", "rounds", "versions", "records", "n"]) {
      if (typeof value[key] === "number") return value[key];
    }
    const nestedArrays = Object.values(value).filter(Array.isArray);
    if (nestedArrays.length) return nestedArrays.reduce((sum, arr) => sum + arr.length, 0);
    const numbers = Object.values(value).filter((v) => typeof v === "number");
    if (numbers.length) return Math.max(...numbers);
  }
  return 0;
}

function integrityItems(value) {
  if (value == null) return [];
  if (Array.isArray(value)) return value;
  if (typeof value === "object") {
    for (const key of ["items", "records", "details", "ids", "versions", "rounds", "files"]) {
      if (Array.isArray(value[key])) return value[key];
    }
    const nestedArrays = Object.values(value).filter(Array.isArray);
    if (nestedArrays.length) return nestedArrays.flat();
    return [value];
  }
  return typeof value === "string" ? [value] : [];
}

function describeIntegrityItem(item) {
  if (item == null) return "";
  if (typeof item !== "object") return String(item);
  const fields = [
    ["id", item.id],
    ["process", item.process_id || item.process || item.process_name],
    ["name", item.name || item.title],
    ["file", item.filename || item.file || item.path],
    ["action", item.action],
    ["category", item.category],
    ["status", item.status],
    ["count", item.count],
    ["ids", Array.isArray(item.ids) ? item.ids.slice(0, 3).join(",") + (item.ids.length > 3 ? "…" : "") : item.ids],
    ["state", item.state],
    ["reason", item.reason || item.message],
    ["at", item.created_at || item.generated_at || item.detached_at || item.orphaned_at],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (fields.length) return fields.map(([key, value]) => `${key}=${value}`).join(" · ");
  try {
    const text = JSON.stringify(item);
    return text.length > 240 ? `${text.slice(0, 237)}…` : text;
  } catch (_) {
    return "无法解析的记录";
  }
}

function integrityGroup(label, raw, count = null) {
  const total = count == null ? integrityCount(raw) : count;
  const items = integrityItems(raw);
  return `<div class="maintenance-group">
    <div class="maintenance-group-head">
      <strong>${esc(label)}</strong>
      <span class="badge ${total ? "badge-orange" : "badge-gray"}">${total}</span>
    </div>
    ${items.length ? `<div class="maintenance-list">${items.slice(0, 12).map((item) =>
      `<div class="maintenance-item">${esc(describeIntegrityItem(item))}</div>`).join("")}
      ${items.length > 12 ? `<div class="maintenance-more">另有 ${items.length - 12} 条明细</div>` : ""}
    </div>` : total ? `<p class="swatch-meta" style="margin:5px 0 0">报告仅提供计数，未附带逐条明细。</p>` : ""}
  </div>`;
}

function normalizeIntegrityReport(report) {
  const active = firstReportValue(report, [
    "summary.processes_active", "processes.active", "counts.active_processes",
    "summary.active_processes", "active_processes", "active_count", "counts.active",
  ]);
  const archived = firstReportValue(report, [
    "summary.processes_archived", "processes.archived", "counts.archived_processes",
    "summary.archived_processes", "archived_processes", "archived_count", "counts.archived",
  ]);
  const recipes = firstReportValue(report, [
    "orphan_recipes", "orphan_derived.recipes", "details.orphan_recipes",
    "details.orphan_derived.recipes", "orphans.recipes", "summary.orphan_recipes", "counts.orphan_recipes",
  ]);
  const compensations = firstReportValue(report, [
    "orphan_compensations", "orphan_derived.compensations", "details.orphan_compensations",
    "details.orphan_derived.compensations", "orphans.compensations",
    "summary.orphan_compensations", "counts.orphan_compensations",
  ]);
  const calibrations = firstReportValue(report, [
    "orphan_calibrations", "orphan_derived.calibrations", "details.orphan_calibrations",
    "details.orphan_derived.calibrations", "orphans.calibrations",
    "summary.orphan_calibrations", "counts.orphan_calibrations",
  ]);
  const passports = firstReportValue(report, [
    "orphan_passports", "orphan_derived.passports", "details.orphan_passports",
    "details.orphan_derived.passports", "orphans.passports",
    "summary.orphan_passports", "counts.orphan_passports",
  ]);
  const measurements = firstReportValue(report, [
    "orphan_measurement_sessions", "orphan_measurements", "details.orphan_measurement_sessions",
    "details.orphan_measurements", "orphans.measurement_sessions", "orphans.measurements",
    "summary.orphan_measurement_sessions", "counts.orphan_measurement_sessions",
  ]);
  const evaluations = firstReportValue(report, [
    "orphan_evaluations", "details.orphan_evaluations", "orphans.evaluations",
    "summary.orphan_evaluations", "counts.orphan_evaluations",
  ]);
  const signoffs = firstReportValue(report, [
    "orphan_signoffs", "details.orphan_signoffs", "orphans.signoffs",
    "summary.orphan_signoffs", "counts.orphan_signoffs",
  ]);
  const missingIcc = firstReportValue(report, [
    "missing_icc", "missing_icc_files", "missing_icc_processes", "details.missing_icc",
    "summary.missing_icc", "counts.missing_icc",
  ]);
  const unreferencedIcc = firstReportValue(report, [
    "unreferenced_icc", "unreferenced_icc_files", "orphan_icc", "details.unreferenced_icc",
    "details.unreferenced_icc_files", "summary.unreferenced_icc", "counts.unreferenced_icc",
  ]);

  const derivedCount = [recipes, compensations, calibrations, passports]
    .reduce((sum, value) => sum + integrityCount(value), 0);
  const derivedFallback = firstReportValue(report, [
    "summary.orphan_derived", "counts.orphan_derived", "orphan_derived",
    "orphan_derived_count",
  ]);
  return {
    generatedAt: report?.generated_at || report?.scanned_at || report?.created_at || "",
    active: integrityCount(active),
    archived: integrityCount(archived),
    recipes, compensations, calibrations, passports,
    measurements, evaluations, signoffs, missingIcc, unreferencedIcc,
    derivedCount: derivedCount || integrityCount(derivedFallback),
    measurementCount: integrityCount(measurements),
    evaluationCount: integrityCount(evaluations),
    signoffCount: integrityCount(signoffs),
    missingIccCount: integrityCount(missingIcc),
    unreferencedIccCount: integrityCount(unreferencedIcc),
  };
}

function renderIntegrityReport(box, report) {
  const data = normalizeIntegrityReport(report);
  const issueTotal = data.derivedCount + data.measurementCount + data.evaluationCount
    + data.signoffCount + data.missingIccCount + data.unreferencedIccCount;
  const stat = (label, value, tone = "") =>
    `<div class="maintenance-stat ${tone}"><b>${value}</b><span>${esc(label)}</span></div>`;
  const sections = [];
  if (data.derivedCount) {
    sections.push(`<section class="maintenance-section">
      <div class="maintenance-section-head"><h4>孤儿派生记录</h4><span class="badge badge-orange">${data.derivedCount}</span></div>
      <div class="maintenance-groups">
        ${integrityGroup("孤儿配方", data.recipes)}
        ${integrityGroup("孤儿补偿", data.compensations)}
        ${integrityGroup("孤儿校准", data.calibrations)}
        ${integrityGroup("孤儿护照", data.passports)}
      </div>
    </section>`);
  }
  if (data.measurementCount || data.evaluationCount) {
    sections.push(`<section class="maintenance-section">
      <div class="maintenance-section-head"><h4>孤儿测量 / 评价</h4><span class="badge badge-orange">${data.measurementCount + data.evaluationCount}</span></div>
      <div class="maintenance-groups">
        ${data.measurementCount ? integrityGroup("孤儿测量会话", data.measurements) : ""}
        ${data.evaluationCount ? integrityGroup("孤儿主观评价", data.evaluations) : ""}
      </div>
      <p class="swatch-meta" style="margin-top:6px">修复策略：解绑但保留审计记录，不删除。</p>
    </section>`);
  }
  if (data.signoffCount) {
    sections.push(`<section class="maintenance-section">
      <div class="maintenance-section-head"><h4>孤儿签样</h4><span class="badge badge-orange">${data.signoffCount}</span></div>
      ${integrityGroup("签样记录", data.signoffs)}
      <p class="swatch-meta" style="margin-top:6px">修复策略：标记 orphaned，不删除签样。</p>
    </section>`);
  }
  if (data.missingIccCount) {
    sections.push(`<section class="maintenance-section">
      <div class="maintenance-section-head"><h4>缺失 ICC</h4><span class="badge badge-red">${data.missingIccCount}</span></div>
      ${integrityGroup("工艺缺少 ICC 文件", data.missingIcc)}
    </section>`);
  }
  if (data.unreferencedIccCount) {
    sections.push(`<section class="maintenance-section">
      <div class="maintenance-section-head"><h4>未引用 ICC</h4><span class="badge badge-orange">${data.unreferencedIccCount}</span></div>
      ${integrityGroup("未被工艺引用的 ICC", data.unreferencedIcc)}
      <p class="swatch-meta" style="margin-top:6px">修复策略：移至 data/orphaned_icc/ 隔离，不直接删除。</p>
    </section>`);
  }

  box.innerHTML = `
    <div class="maintenance-report-head">
      <div>
        <div class="card-title" style="margin:0">完整性摘要</div>
        <p class="swatch-meta" style="margin:2px 0 0">扫描时间：${esc(data.generatedAt ? data.generatedAt.replace("T", " ") : "本次请求")}</p>
      </div>
      <span class="badge ${issueTotal ? "badge-orange" : "badge-green"}">${issueTotal ? `${issueTotal} 个问题` : "未发现问题"}</span>
    </div>
    <div class="maintenance-summary">
      ${stat("Active 工艺", data.active)}
      ${stat("已归档工艺", data.archived)}
      ${stat("孤儿派生", data.derivedCount, data.derivedCount ? "is-warn" : "")}
      ${stat("孤儿测量", data.measurementCount, data.measurementCount ? "is-warn" : "")}
      ${stat("孤儿评价", data.evaluationCount, data.evaluationCount ? "is-warn" : "")}
      ${stat("孤儿签样", data.signoffCount, data.signoffCount ? "is-warn" : "")}
      ${stat("缺失 ICC", data.missingIccCount, data.missingIccCount ? "is-danger" : "")}
      ${stat("未引用 ICC", data.unreferencedIccCount, data.unreferencedIccCount ? "is-warn" : "")}
    </div>
    ${sections.length ? `<div class="maintenance-sections">${sections.join("")}</div>`
      : `<div class="maintenance-clean">${icon("check")}未发现孤儿记录、缺失 ICC 或未引用 ICC，数据完整。</div>`}
    <details class="maintenance-raw">
      <summary>查看原始报告 JSON</summary>
      <pre class="result-code">${esc(JSON.stringify(report, null, 2))}</pre>
    </details>`;
}

function sumCountLeaves(value) {
  if (value == null) return 0;
  if (typeof value === "number") return Math.max(0, value);
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object") {
    return Object.values(value).reduce((sum, item) => sum + sumCountLeaves(item), 0);
  }
  return 0;
}

function renderRepairResult(box, result, dryRun, rescanned = false) {
  const actions = integrityItems(firstReportValue(result, ["actions", "changes", "planned", "results"]) || [])
    .filter((item) => !item || typeof item !== "object" || Number(item.count || 0) > 0);
  const source = dryRun
    ? firstReportValue(result, ["planned"])
    : firstReportValue(result, ["counts"]);
  const groupCount = (key) => sumCountLeaves(readPath(source, key));
  const counts = [
    ["删除孤儿派生", groupCount("deleted")],
    ["解绑测量 / 评价", groupCount("detached")],
    ["标记孤儿签样", groupCount("orphaned")],
    ["隔离未引用 ICC", groupCount("quarantined")],
  ];
  const message = result?.message || result?.summary_text || "";
  box.innerHTML = `
    <div class="maintenance-result ${dryRun ? "is-dry" : "is-applied"}" role="status">
      <div class="maintenance-result-head">
        <span class="badge ${dryRun ? "badge-orange" : "badge-green"}">${dryRun ? "Dry Run · 仅预览" : "已应用"}</span>
        <strong>${dryRun ? "以下为预览结果，未写入磁盘。" : "修复结果已写入磁盘。"}</strong>
      </div>
      <p>${dryRun
        ? "Dry Run 只计算将受影响的记录，不删除、不解绑、不移动任何文件。"
        : `完整性扫描已刷新${rescanned ? "，下方报告为修复后的最新状态" : ""}。`}</p>
      ${message ? `<p class="swatch-meta">${esc(message)}</p>` : ""}
      <div class="maintenance-result-grid">
        ${counts.map(([label, value]) => `<div><b>${value}</b><span>${esc(label)}</span></div>`).join("")}
      </div>
      ${actions.length ? `<details class="maintenance-actions-detail">
        <summary>查看修复动作明细（${actions.length}）</summary>
        <div class="maintenance-list">${actions.slice(0, 30).map((item) =>
          `<div class="maintenance-item">${esc(describeIntegrityItem(item))}</div>`).join("")}</div>
      </details>` : ""}
    </div>`;
}

function buildIntegrity(box, ctx) {
  if (!box) return;
  box.innerHTML = `
    <div class="maintenance-toolbar">
      <div class="maintenance-toolbar-group">
        <button class="btn btn-secondary btn-sm" id="integrity-rescan" type="button">${icon("refresh")}重新扫描</button>
        <button class="btn btn-secondary btn-sm" id="integrity-download" type="button" disabled>${icon("download")}下载 JSON</button>
      </div>
      <div class="maintenance-toolbar-group">
        <button class="btn btn-secondary btn-sm" id="integrity-preview" type="button" disabled>${icon("eye")}预览修复</button>
        <button class="btn btn-danger btn-sm" id="integrity-apply" type="button" disabled>${icon("warn")}应用修复</button>
      </div>
    </div>
    <p class="swatch-meta maintenance-hint">扫描只读；预览修复固定使用 Dry Run，不写盘。应用修复前会再次确认。</p>
    <div id="integrity-repair-result"></div>
    <div id="integrity-report"><div class="spinner"></div></div>
  `;
  const reportBox = box.querySelector("#integrity-report");
  const resultBox = box.querySelector("#integrity-repair-result");
  const rescanBtn = box.querySelector("#integrity-rescan");
  const downloadBtn = box.querySelector("#integrity-download");
  const previewBtn = box.querySelector("#integrity-preview");
  const applyBtn = box.querySelector("#integrity-apply");
  let report = null;

  const setReady = (ready) => {
    downloadBtn.disabled = !ready;
    previewBtn.disabled = !ready;
    applyBtn.disabled = !ready;
  };

  const refresh = async () => {
    report = null;
    setReady(false);
    rescanBtn.disabled = true;
    reportBox.innerHTML = `<div style="display:flex;align-items:center;gap:8px"><span class="spinner"></span><span class="swatch-meta">扫描工艺、派生记录、签样与 ICC 文件…</span></div>`;
    try {
      report = await apiGet("/api/integrity/report", { signal: ctx.signal });
      if (!ctx.isActive() || !box.isConnected) return;
      renderIntegrityReport(reportBox, report);
      setReady(true);
    } catch (e) {
      if (e.name === "AbortError" || !ctx.isActive() || !box.isConnected) return;
      reportBox.innerHTML = `<div class="issue issue-error">${icon("xmark")}<span>${esc(e.message)}</span></div>
        <div style="margin-top:10px"><button class="btn btn-secondary btn-sm" id="integrity-retry">重试</button></div>`;
      reportBox.querySelector("#integrity-retry").addEventListener("click", refresh);
    } finally {
      rescanBtn.disabled = false;
    }
  };

  rescanBtn.addEventListener("click", refresh);
  downloadBtn.addEventListener("click", () => {
    if (!report) return;
    downloadDataUri(
      "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(report, null, 2)),
      "integrity-report.json",
    );
    toast("完整性报告已下载", "ok");
  });

  previewBtn.addEventListener("click", async () => {
    previewBtn.disabled = true;
    previewBtn.innerHTML = `<span class="spinner"></span>预览中…`;
    try {
      const result = await apiPost("/api/integrity/repair", { dry_run: true });
      if (!box.isConnected) return;
      renderRepairResult(resultBox, result, true);
      toast("Dry Run 预览完成，未写入磁盘", "ok");
    } catch (e) {
      toast(e.message, "err");
    } finally {
      previewBtn.disabled = !report;
      previewBtn.innerHTML = `${icon("eye")}预览修复`;
    }
  });

  applyBtn.addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: "应用数据修复？",
      message: "将删除孤儿派生记录、解绑并保留测量/评价、标记孤儿签样，并把未引用 ICC 移至隔离目录。建议先执行 Dry Run 预览。",
      confirmText: "应用修复",
    });
    if (!ok) return;
    applyBtn.disabled = true;
    applyBtn.innerHTML = `<span class="spinner"></span>应用中…`;
    try {
      const result = await apiPost("/api/integrity/repair", { dry_run: false });
      if (!box.isConnected) return;
      toast("数据修复已应用", "ok");
      await refresh();
      renderRepairResult(resultBox, result, false, true);
    } catch (e) {
      toast(e.message, "err");
      applyBtn.disabled = !report;
    } finally {
      applyBtn.innerHTML = `${icon("warn")}应用修复`;
      applyBtn.disabled = !report;
    }
  });

  refresh();
}

/* ---------- 显示器档案与软打样门禁（V0.9 P0-4） ---------- */
const DISP_STATUS_BADGE = {
  ok: "badge badge-green",
  expiring: "badge badge-orange",
  expired: "badge badge-red",
  uncalibrated: "badge badge-gray",
  none: "badge badge-gray",
};

function todayLocal() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function dispStatusBadge(st) {
  return `<span class="${DISP_STATUS_BADGE[st?.status] || "badge badge-gray"}">${esc(st?.status_label || "未校准")}</span>`;
}

function buildDisplay(box, ctx) {
  if (!box) return;
  const rows = displays.map((d) => {
    const st = d.status || {};
    const meas = d.measurement;
    const measLine = meas
      ? `<div class="row-sub">测量：${meas.patches} 块 · 白点 (${(meas.white_xy || []).map((v) => Number(v).toFixed(4)).join(", ") || "—"}) · 对比度 ${meas.contrast_ratio ?? "—"}:1${meas.luminance_cd_m2 != null ? ` · ${meas.luminance_cd_m2} cd/m²` : ""} · ${esc(meas.filename)}</div>`
      : "";
    return `<div class="group-row" data-disp-id="${esc(d.id)}">
      <div class="grow">
        <div class="row-title">${esc(d.name)}${d.is_active ? '<span class="badge badge-blue" style="margin-left:6px">当前</span>' : ""} ${dispStatusBadge(st)}</div>
        <div class="row-sub">${esc(d.model || "型号未填")} · 目标 ${esc(d.targets.white_point)} / ${d.targets.luminance_cd_m2} cd/m² / ${d.targets.contrast_ratio}:1${st.expires_at ? ` · 到期 ${esc(st.expires_at)}` : ""}</div>
        <div class="row-sub">${esc(st.message || "")}</div>
        ${measLine}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${d.is_active ? "" : `<button class="btn btn-secondary btn-sm" data-disp-act="activate">设为当前</button>`}
        <button class="btn btn-secondary btn-sm" data-disp-act="calibrate">校准确认</button>
        <button class="btn btn-secondary btn-sm" data-disp-act="measure">导入测量</button>
        ${meas ? `<button class="btn btn-ghost btn-sm" data-disp-act="export-measure">导出测量</button>` : ""}
        <button class="btn btn-ghost btn-sm" data-disp-act="report">报告</button>
        <button class="btn btn-ghost btn-sm" data-disp-act="delete" style="color:var(--red)">删除</button>
      </div>
    </div>`;
  }).join("");
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <div class="card-desc" style="margin:0">软打样门禁：校准有效（到期前 30 天提醒）才标记为可信预览；系统不生成显示器 ICC。</div>
      <button class="btn btn-sm" id="disp-add">${icon("plus")}新增显示器</button>
    </div>
    ${rows || emptyState({
      iconName: "eye", title: "尚未登记显示器",
      desc: "登记显示器与校准确认后，软打样预览才会标记为可信；未登记时预览仅参考。",
    })}`;
  box.querySelector("#disp-add")?.addEventListener("click", () => openDisplayCreate(box, ctx));
  box.querySelectorAll("[data-disp-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("[data-disp-id]");
      const id = row?.dataset.dispId;
      const act = btn.dataset.dispAct;
      const disp = displays.find((d) => d.id === id);
      const encId = encodeURIComponent(id);
      if (act === "activate") {
        btn.disabled = true;
        try {
          await apiPost(`/api/displays/${encId}/activate`, {});
          toast("已切换当前显示器", "ok");
          refreshDisplayPane(box, ctx);
        } catch (e) {
          btn.disabled = false;
          toast(e.message, "err");
        }
      } else if (act === "calibrate") {
        openDisplayCalibration(disp, box, ctx);
      } else if (act === "measure") {
        openDisplayMeasurement(disp, box, ctx);
      } else if (act === "export-measure") {
        try {
          const resp = await fetch(`/api/displays/${encId}/measurements/latest`);
          if (!resp.ok) {
            let detail = "下载失败";
            try { detail = (await resp.json()).detail || detail; } catch (_) { /* 保留默认 */ }
            throw new Error(detail);
          }
          const text = await resp.text();
          downloadDataUri(
            "data:text/plain;charset=utf-8," + encodeURIComponent(text),
            disp.measurement?.filename || "display-measurement.txt",
          );
        } catch (e) { toast(e.message, "err"); }
      } else if (act === "report") {
        try {
          const report = await apiGet(`/api/displays/${encId}/report`);
          downloadDataUri(
            "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(report, null, 2)),
            `显示器校准-${report.display.name}.json`,
          );
        } catch (e) { toast(e.message, "err"); }
      } else if (act === "delete") {
        const ok = await confirmDanger({
          title: `删除显示器「${disp?.name || ""}」？`,
          message: "档案与校准历史将被移除，软打样门禁退回「未登记」。",
          confirmText: "删除",
        });
        if (!ok) return;
        btn.disabled = true;
        try {
          await apiDelete(`/api/displays/${encId}`);
          toast("显示器档案已删除", "ok");
          refreshDisplayPane(box, ctx);
        } catch (e) {
          btn.disabled = false;
          toast(e.message, "err");
        }
      }
    });
  });
}

async function refreshDisplayPane(box, ctx) {
  try {
    displays = await apiGet("/api/displays", { signal: ctx?.signal });
    if (ctx && !ctx.isActive()) return;
    buildDisplay(box, ctx);
  } catch (e) {
    if (e.name !== "AbortError") toast(e.message, "err");
  }
}

function openDisplayCreate(box, ctx) {
  const m = openModal(`
    <h3 class="card-title">${icon("eye")}新增显示器</h3>
    <div class="field-row">
      <div class="field"><label>名称 *</label><input class="input" id="dp-name" maxlength="80" placeholder="如 Eizo CG2700X（主）"></div>
      <div class="field"><label>型号</label><input class="input" id="dp-model" maxlength="80"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>目标白点</label><select class="input" id="dp-wp"><option>D50</option><option>D55</option><option>D65</option></select></div>
      <div class="field"><label>目标亮度 cd/m²</label><input class="input" id="dp-lum" type="number" value="120" min="40" max="400"></div>
      <div class="field"><label>目标对比度</label><input class="input" id="dp-contrast" type="number" value="1000" min="100"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准日期</label><input class="input" id="dp-cal" type="date" value="${todayLocal()}"></div>
      <div class="field"><label>有效期（月）</label><input class="input" id="dp-months" type="number" value="6" min="1" max="60"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准器</label><input class="input" id="dp-calibrator" maxlength="80" placeholder="i1Display Pro"></div>
      <div class="field"><label>显示器 ICC 名称</label><input class="input" id="dp-profile" maxlength="120" placeholder="Eizo_D50.icc"></div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dp-save">${icon("plus")}创建</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  const saveBtn = m.modal.querySelector("#dp-save");
  saveBtn.addEventListener("click", async () => {
    const name = m.modal.querySelector("#dp-name").value.trim();
    if (!name) return toast("请填写显示器名称", "warn");
    const whitePoint = m.modal.querySelector("#dp-wp").value;
    const calDate = m.modal.querySelector("#dp-cal").value;
    const calibration = calDate ? {
      calibrated_at: calDate,
      valid_months: Number(m.modal.querySelector("#dp-months").value) || 6,
      calibrator: m.modal.querySelector("#dp-calibrator").value.trim(),
      white_point: whitePoint,
      profile_name: m.modal.querySelector("#dp-profile").value.trim(),
    } : null;
    saveBtn.disabled = true;
    try {
      await apiPost("/api/displays", {
        name,
        model: m.modal.querySelector("#dp-model").value.trim(),
        is_active: true,
        targets: {
          white_point: whitePoint,
          luminance_cd_m2: Number(m.modal.querySelector("#dp-lum").value) || 120,
          contrast_ratio: Number(m.modal.querySelector("#dp-contrast").value) || 1000,
        },
        calibration,
      });
      toast("显示器档案已创建", "ok");
      m.close();
      refreshDisplayPane(box, ctx);
    } catch (e) {
      saveBtn.disabled = false;
      toast(e.message, "err");
    }
  });
}

function openDisplayCalibration(disp, box, ctx) {
  if (!disp) return;
  const whitePoint = disp.targets?.white_point || "D50";
  const m = openModal(`
    <h3 class="card-title">${icon("check")}校准确认 · <span style="color:var(--label-2)">${esc(disp.name)}</span></h3>
    <div class="field-row">
      <div class="field"><label>校准日期 *</label><input class="input" id="dc-cal" type="date" value="${todayLocal()}"></div>
      <div class="field"><label>有效期（月）</label><input class="input" id="dc-months" type="number" value="6" min="1" max="60"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准器</label><input class="input" id="dc-calibrator" maxlength="80" value="${esc(disp.calibration?.calibrator || "")}"></div>
      <div class="field"><label>软件</label><input class="input" id="dc-software" maxlength="80" value="${esc(disp.calibration?.software || "")}" placeholder="DisplayCAL"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>实测亮度 cd/m²</label><input class="input" id="dc-lum" type="number" step="0.1" min="0"></div>
      <div class="field"><label>实测对比度</label><input class="input" id="dc-contrast" type="number" step="1" min="0"></div>
    </div>
    <div class="field" style="margin-top:10px"><label>显示器 ICC 名称</label><input class="input" id="dc-profile" maxlength="120" value="${esc(disp.calibration?.profile_name || "")}" placeholder="外部工具生成的显示器 profile"></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dc-save">${icon("check")}保存确认</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  const saveBtn = m.modal.querySelector("#dc-save");
  saveBtn.addEventListener("click", async () => {
    const calDate = m.modal.querySelector("#dc-cal").value;
    if (!calDate) return toast("请填写校准日期", "warn");
    const lum = m.modal.querySelector("#dc-lum").value;
    const contrast = m.modal.querySelector("#dc-contrast").value;
    saveBtn.disabled = true;
    try {
      await apiPost(`/api/displays/${encodeURIComponent(disp.id)}/calibrations`, {
        calibrated_at: calDate,
        valid_months: Number(m.modal.querySelector("#dc-months").value) || 6,
        calibrator: m.modal.querySelector("#dc-calibrator").value.trim(),
        software: m.modal.querySelector("#dc-software").value.trim(),
        white_point: whitePoint,
        luminance_cd_m2: lum === "" ? null : Number(lum),
        contrast_ratio: contrast === "" ? null : Number(contrast),
        profile_name: m.modal.querySelector("#dc-profile").value.trim(),
      });
      toast("校准确认已保存", "ok");
      m.close();
      refreshDisplayPane(box, ctx);
    } catch (e) {
      saveBtn.disabled = false;
      toast(e.message, "err");
    }
  });
}

function openDisplayMeasurement(disp, box, ctx) {
  if (!disp) return;
  const m = openModal(`
    <h3 class="card-title">${icon("upload")}导入屏幕测量 · <span style="color:var(--label-2)">${esc(disp.name)}</span></h3>
    <div class="field"><label>测量文件（CGATS.17 / TSV）*</label><input class="input" id="dm-file" type="file" accept=".txt,.cgats,.ti3,.tsv,.csv"></div>
    <div class="field" style="margin-top:10px"><label>白场绝对亮度 cd/m²（可选）</label><input class="input" id="dm-lum" type="number" step="0.1" min="0" placeholder="如 120；文件头含 LUMINANCE_CD_M2 可留空"></div>
    <div class="issue issue-info" style="margin-top:12px">${icon("info")}<span>需至少 2 个含 XYZ 的色块（白 / 黑）；系统据此推导白点与对比度，并原样留存文件供导出复核。</span></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dm-save">${icon("upload")}导入</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  const saveBtn = m.modal.querySelector("#dm-save");
  saveBtn.addEventListener("click", async () => {
    const input = m.modal.querySelector("#dm-file");
    const file = input.files && input.files[0];
    if (!file) return toast("请选择测量文件", "warn");
    const lum = m.modal.querySelector("#dm-lum").value;
    const query = { filename: file.name };
    if (lum !== "") query.white_luminance_cd_m2 = lum;
    saveBtn.disabled = true;
    try {
      await apiPostRaw(
        `/api/displays/${encodeURIComponent(disp.id)}/measurements`,
        query,
        new Uint8Array(await file.arrayBuffer()),
      );
      toast("屏幕测量已导入", "ok");
      m.close();
      refreshDisplayPane(box, ctx);
    } catch (e) {
      saveBtn.disabled = false;
      toast(e.message, "err");
    }
  });
}
