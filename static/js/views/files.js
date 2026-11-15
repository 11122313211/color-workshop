/* 印前转换视图：阶段流水线（上传预检 → 色彩设定 → 转换 → 完成），阶段前后依赖锁定 */

import { apiGet, apiPost, fileToBase64, downloadDataUri } from "../api.js";
import { icon } from "../icons.js";
import { toast, emptyState } from "../components.js";

const INTENTS = [
  ["relative_colorimetric", "相对比色", "色域内精确匹配，白点跟随纸张"],
  ["perceptual", "感知", "整体压缩保持色彩关系，适合照片"],
  ["saturation", "饱和度", "保持鲜艳度，适合图表/插画"],
  ["absolute_colorimetric", "绝对比色", "模拟纸白，适合打样"],
];

let state = {
  file: null,       // {name, b64}
  report: null,     // preflight report
  processes: [],
  processId: null,
  intent: "relative_colorimetric",
  converted: null,
};

export async function renderFilesView(view) {
  reset();
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">印前转换</h1>
        <p class="page-sub">客户文件预检 → 按工艺 ICC 转换 → 软打样预览与成品下载</p>
      </div>
    </div>
    <div class="stepper" id="stepper"></div>
  `;
  try {
    state.processes = await apiGet("/api/processes");
  } catch (_) {
    state.processes = [];
  }
  renderStepper(view);
}

function reset() {
  state = { file: null, report: null, processes: state.processes || [], processId: null, intent: "relative_colorimetric", converted: null };
}

/* ---------- 阶段定义与渲染 ---------- */
function stageDefs() {
  return [
    { key: "upload", title: "上传与预检", sub: "接入客户文件，自动检查嵌入 ICC / 分辨率 / 主导色" },
    { key: "setup", title: "色彩设定", sub: "选择目标工艺与渲染意图" },
    { key: "convert", title: "执行转换", sub: "经工艺 ICC 转换并生成软打样" },
    { key: "done", title: "完成", sub: "下载成品文件，或继续处理下一个" },
  ];
}

function currentStage() {
  if (!state.file || !state.report) return 0;
  if (!state.processId) return 1;
  if (!state.converted) return 2;
  return 3;
}

function renderStepper(view) {
  const cur = currentStage();
  const defs = stageDefs();
  const el = view.querySelector("#stepper");
  el.innerHTML = defs
    .map((d, i) => {
      const cls = i < cur ? "done" : i === cur ? "current" : "locked";
      const dot = i < cur ? icon("check") : i + 1;
      const body = i === cur || i < cur ? stageBody(d.key, view) : "";
      return `<div class="step ${cls}">
        <div class="step-rail"><div class="step-dot">${dot}</div>${i < defs.length - 1 ? '<div class="step-line"></div>' : ""}</div>
        <div class="step-body">
          <div class="step-title">${d.title}${i < cur ? `<span class="badge badge-green">完成</span>` : ""}</div>
          <div class="step-sub">${d.sub}</div>
          ${body}
        </div>
      </div>`;
    })
    .join("");
  bindStage(view);
}

function stageBody(key, view) {
  if (key === "upload") return uploadBody(view);
  if (key === "setup") return setupBody(view);
  if (key === "convert") return convertBody(view);
  return doneBody(view);
}

/* ---------- 阶段 1：上传与预检 ---------- */
function uploadBody(view) {
  const r = state.report;
  if (!r) {
    return `<div class="step-card">
      <div class="dropzone" id="dz">${icon("upload")}
        <div style="font-weight:600">点击或拖入客户文件</div>
        <div style="font-size:12px;margin-top:4px">支持 PNG / JPEG / TIFF 图像</div>
      </div>
    </div>`;
  }
  const issues = r.issues
    .map(
      (i) =>
        `<div class="issue issue-${i.level}">${icon(i.level === "warn" ? "warn" : i.level === "error" ? "xmark" : "info")}<span>${esc(i.message)}</span></div>`
    )
    .join("");
  const badge = r.status === "pass" ? `<span class="badge badge-green">${icon("check")}预检通过</span>` : `<span class="badge badge-orange">${icon("warn")}有建议项</span>`;
  return `<div class="step-card">
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px">
      ${badge}
      <b>${esc(r.filename || "未命名")}</b>
      <span class="chip chip-mono">${r.format} · ${r.mode} · ${r.size[0]}×${r.size[1]}${r.dpi ? ` · ${r.dpi} DPI` : ""}</span>
    </div>
    <div class="issue-list">${issues || '<div class="issue issue-info">' + icon("check") + "<span>未发现问题</span></div>"}</div>
    ${r.dominant_colors.length ? `<div class="dom-colors">${r.dominant_colors.map((c) => `<div class="swatch swatch-sm" style="background:${c}" title="${c}"></div>`).join("")}<span style="font-size:12px;color:var(--label-2);align-self:center;margin-left:4px">主导色</span></div>` : ""}
    <div style="margin-top:12px"><button class="btn btn-ghost btn-sm" id="reupload">${icon("upload")}换一个文件</button></div>
  </div>`;
}

/* ---------- 阶段 2：色彩设定 ---------- */
function setupBody(view) {
  if (!state.processes.length) {
    return `<div class="step-card">${emptyState({
      iconName: "stack", title: "需要先创建工艺",
      desc: "转换目标由工艺 ICC 决定。先到工艺库创建。",
      action: `<button class="btn btn-sm" id="goto-processes">${icon("plus")}去创建工艺</button>`,
    })}</div>`;
  }
  if (!state.processId) state.processId = state.processes[0].id;
  return `<div class="step-card">
    <div class="field" style="margin-bottom:12px">
      <label>目标工艺</label>
      <select class="input" id="sel-process">
        ${state.processes.map((p) => `<option value="${p.id}" ${p.id === state.processId ? "selected" : ""}>${esc(p.name)} · ${p.icc_meta.device_space} · ${esc(p.icc_meta.description || "")}</option>`).join("")}
      </select>
    </div>
    <div class="field">
      <label>渲染意图</label>
      <div class="seg" id="seg-intent">
        ${INTENTS.map(([v, t]) => `<button data-v="${v}" class="${v === state.intent ? "active" : ""}">${t}</button>`).join("")}
      </div>
      <div class="row-sub" id="intent-desc" style="margin-top:8px">${INTENTS.find((i) => i[0] === state.intent)[2]}</div>
    </div>
    <div style="margin-top:14px"><button class="btn" id="btn-convert">${icon("wand")}执行转换</button></div>
  </div>`;
}

/* ---------- 阶段 3：转换 ---------- */
function convertBody(view) {
  if (!state.converted) {
    return `<div class="step-card" style="display:flex;align-items:center;gap:10px;color:var(--label-2)">
      <span class="spinner"></span>即将开始转换…
    </div>`;
  }
  const c = state.converted;
  return `<div class="step-card">
    <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; align-items:start">
      <div>
        <div style="font-size:13px;font-weight:600;color:var(--label-2);margin-bottom:8px">软打样预览（模拟成品外观）</div>
        <img src="${c.preview_b64}" style="max-width:100%; border-radius:12px; box-shadow:var(--shadow-card)" alt="软打样预览">
      </div>
      <div>
        <div style="font-size:13px;font-weight:600;color:var(--label-2);margin-bottom:8px">输出信息</div>
        <dl class="kv">
          <dt>输出模式</dt><dd>${c.output_mode}</dd>
          <dt>文件格式</dt><dd>${c.file_format}</dd>
          <dt>源 profile</dt><dd>${esc(c.source_profile)}</dd>
        </dl>
        <button class="btn" id="btn-download" style="margin-top:14px">${icon("download")}下载成品文件</button>
      </div>
    </div>
  </div>`;
}

/* ---------- 阶段 4：完成 ---------- */
function doneBody(view) {
  return `<div class="step-card">
    <div class="issue issue-info">${icon("check")}<span>处理完成。可以继续处理下一个客户文件。</span></div>
    <div style="margin-top:12px"><button class="btn btn-secondary btn-sm" id="btn-next">${icon("doc")}处理下一个文件</button></div>
  </div>`;
}

/* ---------- 事件绑定 ---------- */
function bindStage(view) {
  // 阶段 1
  const dz = view.querySelector("#dz");
  if (dz) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*,.tif,.tiff";
    input.style.display = "none";
    dz.appendChild(input);
    dz.addEventListener("click", () => input.click());
    input.addEventListener("change", () => input.files[0] && handleFile(view, input.files[0]));
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("drag");
      if (e.dataTransfer.files[0]) handleFile(view, e.dataTransfer.files[0]);
    });
  }
  const re = view.querySelector("#reupload");
  if (re) re.addEventListener("click", () => { state.file = null; state.report = null; state.converted = null; renderStepper(view); });

  // 阶段 2
  const sel = view.querySelector("#sel-process");
  if (sel) {
    sel.addEventListener("change", () => {
      state.processId = sel.value;
      state.converted = null;
      renderStepper(view);
    });
    view.querySelectorAll("#seg-intent button").forEach((b) => {
      b.addEventListener("click", () => {
        state.intent = b.dataset.v;
        state.converted = null;
        renderStepper(view);
      });
    });
    const go = view.querySelector("#goto-processes");
    if (go) go.addEventListener("click", () => (location.hash = "#/processes"));
    const convBtn = view.querySelector("#btn-convert");
    if (convBtn) convBtn.addEventListener("click", () => doConvert(view));
  }

  // 阶段 3
  const dl = view.querySelector("#btn-download");
  if (dl) {
    dl.addEventListener("click", () => {
      const c = state.converted;
      const ext = c.file_format.toLowerCase() === "tiff" ? "tif" : "png";
      downloadDataUri(c.file_b64, `${(state.file?.name || "output").replace(/\.[^.]+$/, "")}_${c.output_mode}.${ext}`);
    });
  }
  const nx = view.querySelector("#btn-next");
  if (nx) {
    nx.addEventListener("click", () => {
      state.file = null; state.report = null; state.converted = null;
      renderStepper(view);
      view.querySelector("#main")?.scrollTo?.(0, 0);
      document.getElementById("main").scrollTop = 0;
    });
  }
}

async function handleFile(view, file) {
  toast(`正在预检 ${file.name}…`);
  try {
    const b64 = await fileToBase64(file);
    const report = await apiPost("/api/files/preflight", { image_b64: b64, filename: file.name });
    state.file = { name: file.name, b64 };
    state.report = report;
    state.converted = null;
    renderStepper(view);
    toast(report.status === "pass" ? "预检通过" : "预检有建议项，请查看", report.status === "pass" ? "ok" : "warn");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function doConvert(view) {
  const btn = view.querySelector("#btn-convert");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>转换中…`;
  try {
    const c = await apiPost("/api/files/convert", {
      image_b64: state.file.b64,
      process_id: state.processId,
      intent: state.intent,
    });
    state.converted = c;
    renderStepper(view);
    toast("转换完成");
    document.getElementById("main").scrollTo({ top: 600, behavior: "smooth" });
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
    btn.innerHTML = `${icon("wand")}执行转换`;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
