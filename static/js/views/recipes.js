/* 配方预测视图：LAB 输入 → 选工艺 → 迭代反查 → 配方结果 + 实测验证闭环 */

import { apiGet, apiPost, downloadCsv } from "../api.js";
import { icon } from "../icons.js";
import {
  toast, emptyState, statusBadge, labToHex,
  recipeLine, sparkline, disclosure, bindDisclosures, openModal,
} from "../components.js";
import { updateNavCount } from "./shell.js";

let state = {
  processes: [],
  processId: null,
  rows: [],        // {label, L, a, b}
  tolerance: 2.0,
  results: null,   // predict response
};

export async function renderRecipesView(view, params = {}) {
  state.rows = state.rows.length ? state.rows : defaultRows();
  // 工艺详情页「用此工艺预测」带参跳转：预选该工艺（无效 id 会被回落到第一个）
  if (params.query && params.query.process) state.processId = params.query.process;

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">配方预测</h1>
        <p class="page-sub">输入目标 LAB，基于工艺 ICC 反查最优墨量配方 —— 每个配方经过大量正向预测迭代</p>
      </div>
      <button class="btn btn-secondary btn-sm" id="btn-export">${icon("download")}导出 CSV</button>
    </div>

    <div class="section-title">1 · 选择工艺</div>
    <div class="card" id="process-picker"><div class="spinner"></div></div>

    <div class="section-title">2 · 目标 LAB</div>
    <div class="card">
      <div class="lab-table" id="lab-table"></div>
      <div style="display:flex; gap:10px; margin-top:12px; align-items:center; flex-wrap:wrap">
        <button class="btn btn-ghost btn-sm" id="btn-add-row">${icon("plus")}添加颜色</button>
        <span style="flex:1"></span>
        <label style="font-size:13px;color:var(--label-2)">容差 ΔE</label>
        <input class="input" id="tolerance" type="number" step="0.1" min="0.1" style="width:80px" value="2.0">
        <button class="btn" id="btn-predict">${icon("wand")}开始预测</button>
      </div>
      ${disclosure("粘贴批量数据（CSV / 制表符分隔，每行：名称,L,a,b）", `
        <textarea class="input" id="csv-input" rows="4" placeholder="专色红,50,20,5&#10;55,-25,-30&#10;80,5,55"></textarea>
        <div style="margin-top:8px"><button class="btn btn-secondary btn-sm" id="btn-import-csv">导入到表格</button></div>
      `)}
    </div>

    <div id="results-area"></div>
  `;

  bindDisclosures(view);
  await loadProcesses(view);
  renderRows(view);
  bindEvents(view);
  if (state.results) renderResults(view, state.results);
}

function defaultRows() {
  return [
    { label: "专色红", L: "50", a: "20", b: "5" },
    { label: "青", L: "50", a: "-25", b: "-30" },
    { label: "", L: "", a: "", b: "" },
  ];
}

/* ---------- 工艺选择 ---------- */
async function loadProcesses(view) {
  try {
    state.processes = await apiGet("/api/processes");
  } catch (e) {
    view.querySelector("#process-picker").innerHTML = `<div class="issue issue-warn">${icon("warn")}<span>${e.message}</span></div>`;
    return;
  }
  const box = view.querySelector("#process-picker");

  if (!state.processes.length) {
    box.innerHTML = emptyState({
      iconName: "stack", title: "还没有工艺",
      desc: "工艺 = 打印机 + 纸张 + 墨水 + ICC 特性文件。先到工艺库创建一个，再回来预测配方。",
      action: `<button class="btn btn-sm" id="goto-processes">去创建工艺</button>`,
    });
    box.querySelector("#goto-processes").addEventListener("click", () => (location.hash = "#/processes"));
    return;
  }

  if (!state.processId || !state.processes.find((p) => p.id === state.processId)) {
    state.processId = state.processes[0].id;
  }

  box.innerHTML = `<div style="display:flex; gap:10px; flex-wrap:wrap">${state.processes
    .map(
      (p) => `
      <button class="proc-chip ${p.id === state.processId ? "active" : ""}" data-id="${p.id}">
        <span class="badge ${p.icc_meta.device_space === "CMYK" ? "badge-blue" : "badge-gray"}">${p.icc_meta.device_space}</span>
        <span style="font-weight:600">${esc(p.name)}</span>
        ${p.paper ? `<span class="chip">${esc(p.paper)}</span>` : ""}
      </button>`
    )
    .join("")}</div>
  <style>
    .proc-chip { display:inline-flex; align-items:center; gap:8px; padding:9px 14px; border-radius:12px;
      border:1.5px solid var(--separator); background:var(--card); cursor:pointer; font-size:14px;
      transition: border-color .15s, background .15s; color: var(--label); }
    .proc-chip:hover { border-color: var(--accent); }
    .proc-chip.active { border-color: var(--accent); background: var(--accent-soft); }
  </style>`;

  box.querySelectorAll(".proc-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.processId = btn.dataset.id;
      box.querySelectorAll(".proc-chip").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
}

/* ---------- LAB 输入表 ---------- */
function renderRows(view) {
  const table = view.querySelector("#lab-table");
  table.innerHTML = `
    <div class="lab-row lab-head">
      <span>名称</span><span>L* (0-100)</span><span>a*</span><span>b*</span><span></span>
    </div>
    ${state.rows
      .map(
        (r, i) => `
      <div class="lab-row" data-i="${i}">
        <input class="input" data-k="label" value="${esc(r.label)}" placeholder="名称">
        <input class="input" data-k="L" type="number" step="0.1" value="${r.L}" placeholder="L">
        <input class="input" data-k="a" type="number" step="0.1" value="${r.a}" placeholder="a">
        <input class="input" data-k="b" type="number" step="0.1" value="${r.b}" placeholder="b">
        <button class="icon-btn del-row" title="删除">${icon("trash")}</button>
      </div>`
      )
      .join("")}`;

  table.querySelectorAll(".lab-row[data-i]").forEach((row) => {
    const i = +row.dataset.i;
    row.querySelectorAll("[data-k]").forEach((inp) => {
      inp.addEventListener("input", () => {
        state.rows[i][inp.dataset.k] = inp.value;
        if (inp.dataset.k !== "label") paintRow(row, i);
      });
      if (inp.dataset.k !== "label") paintRow(row, i);
    });
    row.querySelector(".del-row").addEventListener("click", () => {
      if (state.rows.length <= 1) return toast("至少保留一行", "warn");
      state.rows.splice(i, 1);
      renderRows(view);
    });
  });
}

function paintRow(row, i) {
  const get = (k) => parseFloat(state.rows[i][k]);
  const vals = ["L", "a", "b"].map(get);
  if (vals.every((v) => !Number.isNaN(v))) {
    row.querySelector('[data-k="L"]').style.borderLeft = `3px solid ${labToHex(vals)}`;
  }
}

function parseCsv(text) {
  return text
    .trim()
    .split(/\r?\n/)
    .map((line) => line.split(/[,\t;]+/).map((s) => s.trim()))
    .filter((cells) => cells.length >= 3)
    .map((cells) => {
      if (cells.length >= 4 && Number.isNaN(parseFloat(cells[0])) === false && !Number.isNaN(parseFloat(cells[1]))) {
        return { label: "", L: cells[0], a: cells[1], b: cells[2] }; // L,a,b
      }
      // 尝试 名称,L,a,b
      const tail = cells.slice(-3);
      const label = cells.slice(0, cells.length - 3).join(" ").trim();
      return { label, L: tail[0], a: tail[1], b: tail[2] };
    })
    .filter((r) => [r.L, r.a, r.b].every((v) => !Number.isNaN(parseFloat(v))));
}

/* ---------- 事件 ---------- */
function bindEvents(view) {
  view.querySelector("#btn-add-row").addEventListener("click", () => {
    state.rows.push({ label: "", L: "", a: "", b: "" });
    renderRows(view);
  });

  view.querySelector("#btn-import-csv").addEventListener("click", () => {
    const rows = parseCsv(view.querySelector("#csv-input").value);
    if (!rows.length) return toast("未解析到有效数据行", "warn");
    state.rows = rows;
    renderRows(view);
    toast(`已导入 ${rows.length} 个颜色`);
  });

  view.querySelector("#tolerance").addEventListener("input", (e) => {
    state.tolerance = parseFloat(e.target.value) || 2.0;
  });

  view.querySelector("#btn-predict").addEventListener("click", () => predict(view));
  view.querySelector("#btn-export").addEventListener("click", () => exportCsv());
}

async function predict(view) {
  const btn = view.querySelector("#btn-predict");
  const targets = state.rows
    .map((r) => ({
      label: r.label,
      lab: [parseFloat(r.L), parseFloat(r.a), parseFloat(r.b)],
    }))
    .filter((t) => t.lab.every((v) => !Number.isNaN(v)));

  if (!targets.length) return toast("请先填写至少一个有效的 LAB 颜色", "warn");
  if (!state.processId) return toast("请先选择工艺", "warn");

  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>预测中…`;
  const area = view.querySelector("#results-area");
  area.innerHTML = `<div class="card" style="display:flex;align-items:center;gap:10px;color:var(--label-2)">
    <span class="spinner"></span>正在迭代预测 ${targets.length} 个颜色…（每个颜色约需数百次正向预测）
  </div>`;

  try {
    const resp = await apiPost("/api/recipes/predict", {
      process_id: state.processId,
      targets: targets.map((t) => ({ label: t.label || "", lab: t.lab })),
      tolerance: state.tolerance,
      method: "cie2000",
    });
    state.results = resp;
    renderResults(view, resp);
  } catch (e) {
    area.innerHTML = `<div class="card"><div class="issue issue-error">${icon("xmark")}<span>${esc(e.message)}</span></div></div>`;
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${icon("wand")}开始预测`;
  }
}

/* ---------- 结果渲染 ---------- */
function renderResults(view, resp) {
  const area = view.querySelector("#results-area");
  const rowsHtml = resp.results
    .map((r, idx) => resultCard(r, idx))
    .join("");

  area.innerHTML = `
    <div class="section-title">3 · 预测结果 <span class="chip chip-mono">${esc(resp.process_name)} · ${resp.device_space} · ${resp.total_predictions} 次正向预测 · ${resp.elapsed_ms}ms</span></div>
    ${rowsHtml}
    <div id="verify-area"></div>
  `;

  resp.results.forEach((r, idx) => {
    // 实测验证闭环
    const card = area.querySelector(`#result-${idx}`);
    const verBtn = card.querySelector(".btn-verify");
    verBtn.addEventListener("click", () => openVerifyModal(view, r, idx));
    // 打开验证历史折叠
    const hist = card.querySelector(".disclosure");
    if (hist) bindDisclosures(card);
  });
}

function resultCard(r, idx) {
  const hexTarget = labToHex(r.target_lab);
  const hexPred = labToHex(r.predicted_lab);
  const verify = r._lastVerification; // 前端暂存
  return `
  <div class="card" id="result-${idx}">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:14px">
      <div class="card-title">${r.label ? esc(r.label) : `颜色 ${idx + 1}`}
        ${r.compensation && r.compensation.applied ? `<span class="chip" title="实测闭环沉淀的工艺级墨量偏置已作用于本次预测">补偿 n=${r.compensation.n}</span>` : ""}
      </div>
      ${statusBadge(r.delta_e, r.tolerance)}
    </div>

    <div class="compare-grid">
      <div class="swatch-row">
        <div>
          <div class="swatch" style="background:${hexTarget}"></div>
          <div class="swatch-label">目标<br><span class="swatch-meta">Lab ${fmtLab(r.target_lab)}</span></div>
        </div>
        <div style="color:var(--label-3)">${icon("chevronR")}</div>
        <div>
          <div class="swatch" style="background:${hexPred}"></div>
          <div class="swatch-label">预测<br><span class="swatch-meta">Lab ${fmtLab(r.predicted_lab)}</span></div>
        </div>
      </div>
      <div class="divider-v"></div>
      <div style="flex:1; min-width:240px">
        <div class="swatch-meta" style="margin-bottom:6px">分色值（设备值）</div>
        ${recipeLine(r.recipe)}
        <div class="swatch-meta" style="margin-top:6px">${esc(r.recipe_meta?.note || "设备通道分色值，不是基础油墨调墨配方")}</div>
      </div>
    </div>

    <div class="spark-wrap" style="margin-top:14px">
      ${sparkline(r.iterations, { tolerance: r.tolerance })}
      <span>ΔE 迭代轨迹：${r.iterations.map((v) => v.toFixed(2)).join(" → ")} · 共 ${r.predictions} 次正向预测（粗查表 ${r.lut_points}）</span>
    </div>

    <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap">
      <button class="btn btn-secondary btn-sm btn-verify">${icon("target")}实测验证</button>
      ${r.recipe_id ? `<a class="btn btn-secondary btn-sm" href="/api/recipes/${encodeURIComponent(r.recipe_id)}/export?format=md" download>${icon("download")}导出配方单</a>
        <a class="btn btn-ghost btn-sm" href="/api/recipes/${encodeURIComponent(r.recipe_id)}/export?format=json" download>JSON</a>` : ""}
    </div>
    ${verify ? verificationBlock(verify) : ""}
  </div>`;
}

function verificationBlock(v) {
  const c = v.components;
  const compStatus = v.compensation_status || {};
  const compChip = compStatus.applied
    ? `<span class="chip" style="background:var(--green-soft); color:var(--green)">补偿已沉淀 n=${compStatus.n}</span>`
    : compStatus.skipped_reason
      ? `<div class="issue issue-warn" style="margin-top:10px">${icon("warn")}<span>${esc(compStatus.skipped_reason)}</span></div>`
      : "";
  const interp = [];
  if (c) {
    if (Math.abs(c.dL) >= 0.5) interp.push(`实测偏${c.dL > 0 ? "亮" : "暗"}（ΔL ${c.dL > 0 ? "+" : ""}${c.dL}）`);
    if (Math.abs(c.dC) >= 0.5) interp.push(`饱和度偏${c.dC > 0 ? "高" : "低"}（ΔC ${c.dC > 0 ? "+" : ""}${c.dC}）`);
    if (Math.abs(c.dh) >= 1) interp.push(`色相偏移 ${c.dh > 0 ? "+" : ""}${c.dh}°`);
    if (!interp.length) interp.push("各分量均在阈值内，偏差以明度/彩度微差为主");
  }
  return `<div class="disclosure open" style="margin-top:14px">
    <div class="disclosure-body">
      <div class="issue ${v.improved ? "issue-info" : "issue-warn"}">${icon(v.improved ? "check" : "warn")}
        <span>实测 ΔE <b>${v.delta_e_measured}</b> → 修正配方后预测 ΔE <b>${v.delta_e_corrected}</b>（模型误差 ${v.delta_e_model_error}）${v.improved ? "，建议采用修正配方" : "，修正收益有限，建议重测"}</span>
      </div>
      ${c ? `<p class="swatch-meta" style="margin-top:8px">分量（实测−预测）：ΔL ${c.dL} · Δa ${c.da} · Δb ${c.db} · ΔC ${c.dC} · Δh ${c.dh}° · ΔE76 ${c.dE76}${interp.length ? ` — ${interp.join("，")}` : ""}</p>` : ""}
      ${compChip}
      <div style="margin-top:10px">${recipeLine(v.corrected_recipe)}</div>
    </div>
  </div>`;
}

function openVerifyModal(view, r, idx) {
  const proc = state.processes.find((p) => p.id === state.processId);
  const procChip = proc
    ? `<span class="chip chip-mono">${esc(proc.name)}${proc.paper ? ` · ${esc(proc.paper)}` : ""}</span>`
    : "";
  const m = openModal(`
    <h3 class="card-title">${icon("target")}实测验证 · ${r.label || `颜色 ${idx + 1}`} ${procChip}</h3>
    <p class="card-desc">将当前配方打印后，用分光光度计测量色块，把实测 LAB 填入下方。系统将计算偏差并给出修正配方（模型误差补偿 p' = t − (m − p)）。</p>
    <div class="lab-row">
      <span class="lab-head">实测 L*</span><span class="lab-head">a*</span><span class="lab-head">b*</span><span></span><span></span>
    </div>
    <div class="lab-row">
      <input class="input" id="vm-l" type="number" step="0.1" placeholder="L">
      <input class="input" id="vm-a" type="number" step="0.1" placeholder="a">
      <input class="input" id="vm-b" type="number" step="0.1" placeholder="b">
      <span></span><span></span>
    </div>
    <div class="field" id="vm-sess-field" style="margin-top:12px">
      <label>关联测量会话（可选 — 补偿门禁）</label>
      <select id="vm-sess" class="input"><option value="">不关联（修正照常，补偿沉淀不受限）</option></select>
      <p class="swatch-meta" style="margin-top:4px">关联后仅当会话含 <b>verified</b> 测量（复测一致 + 交叉校验通过）时，修正才会沉淀进工艺补偿模型。</p>
    </div>
    <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:16px">
      <button class="btn btn-ghost" id="vm-cancel">取消</button>
      <button class="btn" id="vm-ok">提交验证</button>
    </div>
  `);

  // 测量会话下拉（有 verified 记录的会话优先展示）
  apiGet("/api/qc/measurements").then((sessions) => {
    const sel = m.modal.querySelector("#vm-sess");
    for (const s of sessions) {
      const hasVerified = (s.stats || {}).verified > 0;
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = `${s.instrument} · ${s.condition} · ${hasVerified ? `verified ${s.stats.verified}` : "无 verified"}${hasVerified ? " ✓" : ""}`;
      sel.appendChild(opt);
    }
  }).catch(() => {
    m.modal.querySelector("#vm-sess-field").hidden = true;
  });

  // 预填当前预测值，方便演示
  m.modal.querySelector("#vm-l").value = r.predicted_lab[0];
  m.modal.querySelector("#vm-a").value = r.predicted_lab[1];
  m.modal.querySelector("#vm-b").value = r.predicted_lab[2];

  m.modal.querySelector("#vm-cancel").addEventListener("click", m.close);
  m.modal.querySelector("#vm-ok").addEventListener("click", async () => {
    const lab = [
      parseFloat(m.modal.querySelector("#vm-l").value),
      parseFloat(m.modal.querySelector("#vm-a").value),
      parseFloat(m.modal.querySelector("#vm-b").value),
    ];
    if (lab.some(Number.isNaN)) return toast("请填写完整实测 LAB", "warn");
    try {
      // 找到该结果对应的配方记录（同工艺 + 同目标 LAB）
      const records = await apiGet(`/api/recipes?process_id=${state.processId}`);
      const doc = records.find(
        (d) => d.target_lab[0] === r.target_lab[0] && d.target_lab[1] === r.target_lab[1] && d.target_lab[2] === r.target_lab[2]
      );
      if (!doc) throw new Error("未找到对应配方记录，请重新预测");
      const v = await apiPost("/api/recipes/verify", {
        recipe_id: doc.id,
        measured_lab: lab,
        measurement_session_id: m.modal.querySelector("#vm-sess").value || "",
      });
      r._lastVerification = v;
      m.close();
      renderResults(view, state.results);
      toast(`验证完成：ΔE ${v.delta_e_measured} → ${v.delta_e_corrected}`, v.improved ? "ok" : "warn");
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

function exportCsv() {
  if (!state.results) return toast("暂无预测结果", "warn");
  const proc = state.results.device_space || "CMYK";
  const channels = Object.keys(state.results.results[0].recipe);
  const header = [
    "名称", "目标L", "目标a", "目标b", "预测L", "预测a", "预测b", "ΔE2000",
    ...channels.map((c) => c + "%"),
    "recipe_kind",
  ].join(",");
  const lines = state.results.results.map((r) =>
    [
      `"${(r.label || "").replace(/"/g, '""')}"`,
      ...r.target_lab.map((v) => Math.round(v * 100) / 100),
      ...r.predicted_lab.map((v) => Math.round(v * 100) / 100),
      r.delta_e,
      ...channels.map((c) => r.recipe[c]),
      r.recipe_kind || "device_values",
    ].join(",")
  );
  downloadCsv([header, ...lines].join("\n"), `配方_${state.results.process_name}.csv`);
  toast("CSV 已导出");
}

function fmtLab(lab) {
  return lab.map((v) => Math.round(v * 10) / 10).join(", ");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
