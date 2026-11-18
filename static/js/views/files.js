/* 印前转换视图：阶段流水线（上传预检 → 色彩设定 → 转换 → 完成），阶段前后依赖锁定 */

import { apiGet, apiPost, apiPostRaw, fileToBase64, downloadDataUri } from "../api.js";
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
  pdfx: null,           // PDF/X-4 封装结果
  pdfxProcessId: null,
  pdfxStrict: true,
  displayStatus: null,  // 软打样显示器校准状态
};

export async function renderFilesView(view) {
  reset();
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">印前转换</h1>
        <p class="page-sub">客户文件预检 → 色彩转换 / PDF/X-4 封装 → 成品下载</p>
      </div>
    </div>
    <div class="stepper" id="stepper"></div>
  `;
  try {
    const [procs, disp] = await Promise.all([
      apiGet("/api/processes"),
      apiGet("/api/displays/active-status").catch(() => null),
    ]);
    state.processes = procs;
    state.displayStatus = disp;
  } catch (_) {
    state.processes = [];
  }
  renderStepper(view);
}

function reset() {
  state = {
    file: null, report: null, processes: state.processes || [], processId: null,
    intent: "relative_colorimetric", converted: null,
    pdfx: null, pdfxProcessId: null, pdfxStrict: true,
    displayStatus: state.displayStatus || null,
  };
}

/* ---------- 阶段定义与渲染 ---------- */
function isPdfReport() {
  return !!(state.report && state.report.format === "PDF");
}

function stageDefs() {
  if (isPdfReport()) {
    return [
      { key: "upload", title: "上传与预检", sub: "接入客户 PDF，检查字体 / 出血 / 有效 DPI / 开本" },
      { key: "setup", title: "PDF/X 封装", sub: "选择输出意图与严格模式，写入 OutputIntent / XMP / 盒子" },
      { key: "done", title: "完成", sub: "下载 PDF/X-4 文件，或继续处理下一个" },
    ];
  }
  return [
    { key: "upload", title: "上传与预检", sub: "接入客户文件，自动检查嵌入 ICC / 分辨率 / 主导色" },
    { key: "setup", title: "色彩设定", sub: "选择目标工艺与渲染意图" },
    { key: "convert", title: "执行转换", sub: "经工艺 ICC 转换并生成软打样" },
    { key: "done", title: "完成", sub: "下载成品文件，或继续处理下一个" },
  ];
}

function currentStage() {
  if (!state.file || !state.report) return 0;
  if (isPdfReport()) return state.pdfx ? 2 : 1;
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
  if (key === "setup") return isPdfReport() ? pdfxBody(view) : setupBody(view);
  if (key === "convert") return convertBody(view);
  return isPdfReport() ? pdfxDoneBody(view) : doneBody(view);
}

/* ---------- 阶段 1：上传与预检 ---------- */
function uploadBody(view) {
  const r = state.report;
  if (!r) {
    return `<div class="step-card">
      <div class="dropzone" id="dz">${icon("upload")}
        <div style="font-weight:600">点击或拖入客户文件</div>
        <div style="font-size:12px;margin-top:4px">支持 PNG / JPEG / TIFF 图像与 PDF 文档</div>
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
  const isPdf = r.format === "PDF";
  const fe = isPdf && r.pdf && r.pdf.font_embedding;
  const bleed = isPdf && r.pdf ? (r.pdf.bleed || {}) : {};
  const ed = isPdf && r.pdf ? (r.pdf.effective_dpi || {}) : {};
  const pf = isPdf && r.pdf ? r.pdf.page_format : null;
  const fontText = fe
    ? `${fe.total} 个 · ${fe.embedded} 嵌入${fe.not_embedded.length ? ` · <span class="badge badge-orange">${fe.not_embedded.length} 未嵌入</span>` : ` <span class="badge badge-green">全部嵌入</span>`}`
    : "";
  const bleedText = bleed.checked
    ? (bleed.min_mm == null ? "—" : `${bleed.min_mm} mm ${bleed.ok ? `<span class="badge badge-green">达标</span>` : `<span class="badge badge-red">不足</span>`} <span style="color:var(--label-3)">${bleed.source || ""}</span>`)
    : "未能判定";
  const dpiText = ed.checked
    ? (ed.min == null ? "无位图对象" : `${ed.min} DPI ${ed.low_count ? `<span class="badge badge-orange">${ed.low_count} 处不足</span>` : `<span class="badge badge-green">达标</span>`}`)
    : "未能解析绘制尺寸";
  const formatText = pf
    ? (pf.match ? `${pf.name} · ${pf.size_mm[0]}×${pf.size_mm[1]} mm` : `异形 · ${pf.size_mm[0]}×${pf.size_mm[1]} mm`)
    : "—";
  const pdfBlock = isPdf && r.pdf ? `
    <div style="display:flex; gap:14px; margin-top:12px; flex-wrap:wrap; align-items:flex-start">
      ${r.thumbnail_b64 ? `<img src="${r.thumbnail_b64}" alt="PDF 首页缩略图" style="max-width:180px; max-height:240px; border:1px solid var(--separator); border-radius:10px">` : ""}
      <dl class="kv" style="flex:1; min-width:220px">
        <dt>页数</dt><dd>${r.pages}</dd>
        <dt>页面尺寸</dt><dd>${r.pdf.page_sizes_mm.map((s) => `${s[0]}×${s[1]} mm`).slice(0, 3).join(" · ")}${r.pdf.page_sizes_mm.length > 3 ? " …" : ""}</dd>
        <dt>成品开本</dt><dd>${formatText}</dd>
        <dt>出血</dt><dd>${bleedText}</dd>
        <dt>有效分辨率</dt><dd>${dpiText}</dd>
        <dt>输出意图</dt><dd>${r.pdf.has_output_intent ? `<span class="badge badge-green">已嵌入</span>` : `<span class="badge badge-orange">未嵌入</span>`}</dd>
        <dt>图像对象</dt><dd>${r.pdf.images.length ? r.pdf.images.slice(0, 4).map((im) => `第${im.page}页 ${im.px[0]}×${im.px[1]} ${esc(im.colorspace)}${im.effective_dpi != null ? ` · ${im.effective_dpi} DPI` : ""}`).join("；") + (r.pdf.images.length > 4 ? "…" : "") : "无位图对象"}</dd>
        <dt>专色</dt><dd>${r.pdf.spot_colors.length ? r.pdf.spot_colors.map(esc).join("、") : "—"}</dd>
        <dt>字体</dt><dd>${fontText || `${r.pdf.fonts} 个字体对象`}${fe && fe.not_embedded.length ? `<div class="row-sub" style="margin-top:4px">未嵌入：${fe.not_embedded.slice(0, 4).map((f) => esc(f.name)).join("、")}${fe.not_embedded.length > 4 ? " …" : ""}</div>` : ""}${r.pdf.transparency ? '<div class="row-sub" style="margin-top:4px">含透明对象</div>' : ""}</dd>
      </dl>
    </div>` : "";
  const chip = isPdf
    ? `<span class="chip chip-mono">PDF · ${r.pages} 页 · ${r.size[0]}×${r.size[1]} mm</span>`
    : `<span class="chip chip-mono">${r.format} · ${r.mode} · ${r.size[0]}×${r.size[1]}${r.dpi ? ` · ${r.dpi} DPI` : ""}</span>`;
  return `<div class="step-card">
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px">
      ${badge}
      <b>${esc(r.filename || "未命名")}</b>
      ${chip}
    </div>
    <div class="issue-list">${issues || '<div class="issue issue-info">' + icon("check") + "<span>未发现问题</span></div>"}</div>
    ${pdfBlock}
    ${!isPdf && r.dominant_colors.length ? `<div class="dom-colors">${r.dominant_colors.map((c) => `<div class="swatch swatch-sm" style="background:${c}" title="${c}"></div>`).join("")}<span style="font-size:12px;color:var(--label-2);align-self:center;margin-left:4px">主导色</span></div>` : ""}
    <div style="margin-top:12px"><button class="btn btn-ghost btn-sm" id="reupload">${icon("upload")}换一个文件</button></div>
  </div>`;
}

/* ---------- 阶段 2（PDF）：PDF/X-4 结构封装 ---------- */
function pdfxBody(view) {
  if (!isPdfReport()) return "";
  if (state.pdfx) {
    const c = state.pdfx;
    return `<div class="step-card" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <span class="badge badge-green">${icon("check")}已封装</span>
      <span style="font-size:13px">输出意图：${esc(c.output_intent.identifier)} · ${c.output_intent.channels} 通道</span>
      <span style="flex:1"></span>
      <button class="btn btn-secondary btn-sm" id="pdfx-reset">${icon("swap")}重新封装</button>
    </div>`;
  }
  const procs = (state.processes || []).filter(
    (p) => p.icc_meta && p.icc_meta.device_space === "CMYK",
  );
  if (!procs.length) {
    return `<div class="step-card">${emptyState({
      iconName: "stack", title: "需要 CMYK 工艺",
      desc: "PDF/X 输出意图由 CMYK 工艺 ICC 决定。先到工艺库创建一个 CMYK 工艺。",
      action: `<button class="btn btn-sm" id="goto-processes">${icon("plus")}去创建工艺</button>`,
    })}</div>`;
  }
  if (!state.pdfxProcessId || !procs.some((p) => p.id === state.pdfxProcessId)) {
    state.pdfxProcessId = procs[0].id;
  }
  const proc = procs.find((p) => p.id === state.pdfxProcessId) || procs[0];
  return `<div class="step-card">
    <div class="field">
      <label>输出意图（目标工艺 ICC）</label>
      <select class="input" id="sel-pdfx-process">
        ${procs.map((p) => `<option value="${p.id}" ${p.id === proc.id ? "selected" : ""}>${esc(p.name)} · ${esc(p.icc_meta.description || "")}</option>`).join("")}
      </select>
      <div style="font-size:12px;color:var(--label-2);margin-top:6px">
        成品：${esc(proc.name)} · ${proc.icc_meta.device_space} · ICC 指纹 <span class="chip chip-mono">${(proc.icc_sha256 || "").slice(0, 8)}</span>
      </div>
    </div>
    <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;font-size:13px;line-height:1.5">
      <input type="checkbox" id="pdfx-strict" ${state.pdfxStrict ? "checked" : ""} style="margin-top:2px">
      <span>严格模式：字体未嵌入或存在未标定 RGB 图像时拒绝封装（PDF/X 要求字体全部嵌入）</span>
    </label>
    <div class="issue issue-info" style="margin-top:12px">${icon("info")}<span>写入 OutputIntent、XMP GTS_PDFXVersion、TrimBox/BleedBox；不改变页面内容与色彩空间。结构级合规，交付前仍需 Acrobat Preflight 复核。</span></div>
    <div style="margin-top:14px"><button class="btn" id="btn-pdfx">${icon("doc")}封装为 PDF/X-4</button></div>
  </div>`;
}

/* ---------- 阶段 3（PDF）：下载成品 ---------- */
function pdfxDoneBody(view) {
  const c = state.pdfx;
  if (!c) return "";
  const checks = (c.checks || [])
    .map((i) => `<div class="issue issue-${i.level}">${icon(i.level === "warn" ? "warn" : i.level === "error" ? "xmark" : "info")}<span>${esc(i.message)}</span></div>`)
    .join("");
  const badge = c.status === "pass"
    ? `<span class="badge badge-green">${icon("check")}结构完整</span>`
    : `<span class="badge badge-orange">${icon("warn")}有确认项</span>`;
  return `<div class="step-card">
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap">
      ${badge}
      <b>${esc(c.filename)}</b>
      <span class="chip chip-mono">${c.mode} · ${c.pages} 页</span>
    </div>
    <dl class="kv">
      <dt>输出意图</dt><dd>${esc(c.output_intent.identifier)} · ${c.output_intent.channels} 通道</dd>
      <dt>ICC 指纹</dt><dd><span class="chip chip-mono">${esc(c.output_intent.icc_sha256)}</span></dd>
      <dt>合规状态</dt><dd>${c.compliant ? "结构硬门禁通过（字体 / RGB / 解析）" : "结构已写入，存在未达标项"}</dd>
    </dl>
    <div class="issue-list" style="margin-top:10px">${checks || '<div class="issue issue-info">' + icon("check") + "<span>未发现额外问题</span></div>"}</div>
    ${(c.notes || []).map((n) => `<div class="issue issue-info" style="margin-top:6px">${icon("info")}<span>${esc(n)}</span></div>`).join("")}
    <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap">
      <button class="btn" id="pdfx-download">${icon("download")}下载 ${esc(c.filename)}</button>
      <button class="btn btn-secondary btn-sm" id="reupload">${icon("upload")}换一个文件</button>
    </div>
  </div>`;
}

function dataUrlToBytes(dataUrl) {
  const b64 = dataUrl.startsWith("data:") ? dataUrl.slice(dataUrl.indexOf(",") + 1) : dataUrl;
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function doPdfx(view) {
  const btn = view.querySelector("#btn-pdfx");
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>封装中…`;
  try {
    const result = await apiPostRaw("/api/files/pdfx", {
      process_id: state.pdfxProcessId,
      filename: state.file.name,
      strict: state.pdfxStrict ? "1" : "0",
    }, dataUrlToBytes(state.file.b64));
    state.pdfx = result;
    renderStepper(view);
    toast(result.status === "pass" ? "PDF/X-4 已生成" : "已生成，请查看检查项",
      result.status === "pass" ? "ok" : "warn");
    document.getElementById("main")?.scrollTo?.({ top: 500, behavior: "smooth" });
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
    btn.innerHTML = `${icon("doc")}封装为 PDF/X-4`;
  }
}

/* ---------- 阶段 2（图像）：色彩设定 ---------- */
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
    ${state.displayStatus ? `<div class="issue issue-${state.displayStatus.calibrated ? "info" : "warn"}" style="margin-top:12px">
      ${icon(state.displayStatus.calibrated ? "check" : "warn")}<span>${esc(state.displayStatus.message)}${state.displayStatus.expires_at ? `（到期 ${esc(state.displayStatus.expires_at)}）` : ""}</span>
    </div>` : ""}
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
  const sp = c.softproof || {};
  const spLine = sp.message
    ? `<div class="issue issue-${sp.calibrated ? "info" : "warn"}" style="margin-bottom:12px">${icon(sp.calibrated ? "check" : "warn")}<span>${esc(sp.message)}${sp.expires_at ? `（到期 ${esc(sp.expires_at)}）` : ""}</span></div>`
    : "";
  return `<div class="step-card">
    ${spLine}
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
    input.accept = "image/*,.tif,.tiff,.pdf,application/pdf";
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
  if (re) re.addEventListener("click", () => {
    state.file = null; state.report = null; state.converted = null;
    state.pdfx = null; state.pdfxProcessId = null;
    renderStepper(view);
  });

  const go = view.querySelector("#goto-processes");
  if (go) go.addEventListener("click", () => (location.hash = "#/processes"));

  // PDF/X 封装（PDF 流程）
  const pxSel = view.querySelector("#sel-pdfx-process");
  if (pxSel) {
    pxSel.addEventListener("change", () => {
      state.pdfxProcessId = pxSel.value;
      state.pdfx = null;
      renderStepper(view);
    });
  }
  const pxStrict = view.querySelector("#pdfx-strict");
  if (pxStrict) {
    pxStrict.addEventListener("change", () => { state.pdfxStrict = pxStrict.checked; });
  }
  const pxBtn = view.querySelector("#btn-pdfx");
  if (pxBtn) pxBtn.addEventListener("click", () => doPdfx(view));
  const pxReset = view.querySelector("#pdfx-reset");
  if (pxReset) {
    pxReset.addEventListener("click", () => {
      state.pdfx = null;
      renderStepper(view);
    });
  }
  const pxDl = view.querySelector("#pdfx-download");
  if (pxDl) {
    pxDl.addEventListener("click", () => {
      if (state.pdfx) downloadDataUri(state.pdfx.file_b64, state.pdfx.filename);
    });
  }

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
      state.pdfx = null; state.pdfxProcessId = null;
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
    state.pdfx = null;
    state.pdfxProcessId = null;
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
      display_id: state.displayStatus?.display_id || "",
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
