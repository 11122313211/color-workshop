/* 监控视图（V0.4/V0.5）：ΔE 趋势总览 + 分工艺漂移告警 + 测量数据可靠性闭环。
 * 数据源：GET /api/recipes/stats + GET /api/qc/measurements。
 * V0.5 测量数据卡：会话登记（仪器/条件/校准/背衬/复测模式）→ 导入 → 五条规则分级
 * （verified/warning/rejected），rejected 数据可查不可用。
 */

import { apiGet, apiPostRaw, apiDelete } from "../api.js";
import { icon } from "../icons.js";
import {
  emptyState, errorState, skeletonCards, esc, openModal, toast, confirmDanger,
} from "../components.js";

const TOL_DEFAULT = 2.0;

/* ---- V0.5 测量数据：可靠性分级徽标 ---- */
function reliabilityBadge(rel) {
  const map = {
    verified: { cls: "badge-green", icon: "check", text: "verified" },
    warning: { cls: "badge-orange", icon: "warn", text: "warning" },
    rejected: { cls: "badge-red", icon: "xmark", text: "rejected" },
  };
  const m = map[rel] || map.warning;
  return `<span class="badge ${m.cls}">${icon(m.icon)}${m.text}</span>`;
}

function sessionRow(s, onDetail) {
  const st = s.stats || { verified: 0, warning: 0, rejected: 0 };
  const repeatChip = s.repeat_mode === "repeat"
    ? `<span class="chip" style="background:var(--green-soft); color:var(--green)">复测组</span>`
    : `<span class="chip">单次测量</span>`;
  const condChip = `<span class="chip chip-mono">${esc(s.condition || "M1")}</span>`;
  return `
    <div class="qc-session" data-sid="${esc(s.id)}">
      <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
        <b style="color:var(--label)">${esc(s.instrument)}</b>
        ${condChip}${repeatChip}
        <span class="swatch-meta">校准 ${esc(s.calibrated_at || "未登记")}${s.backing ? " · 背衬 " + esc(s.backing) : ""}</span>
        <span style="flex:1"></span>
        <span class="badge badge-green">${st.verified} verified</span>
        <span class="badge badge-orange">${st.warning} warning</span>
        <span class="badge badge-red">${st.rejected} rejected</span>
        <button class="btn btn-secondary btn-sm qc-session-toggle" type="button">${icon("doc")}明细</button>
      </div>
      <div class="qc-session-records" hidden></div>
    </div>`;
}

function recordRow(r, sid) {
  const reasons = (r.reasons || []).slice(0, 3).map(esc);
  return `
    <div style="display:flex; align-items:flex-start; gap:10px; padding:8px 0; border-top:1px solid var(--separator)">
      ${reliabilityBadge(r.reliability)}
      <div style="flex:1; min-width:0">
        <div style="font-weight:600; font-size:13px; color:var(--label)">${esc(r.filename)}
          <span class="swatch-meta">${esc(r.kind)} · ${r.patch_count} 块${r.density_ok ? "" : " · 密度通道不可用"}</span></div>
        ${reasons.length ? `<div class="swatch-meta" style="margin-top:2px">${reasons.join("；")}${(r.reasons || []).length > 3 ? " …" : ""}</div>` : ""}
      </div>
      <button class="btn btn-ghost btn-sm qc-record-del" data-sid="${esc(sid)}" data-rid="${esc(r.id)}" type="button" title="删除记录">${icon("trash")}</button>
    </div>`;
}

/* ---- 测量数据卡（V0.5 可信测量闭环） ---- */
async function renderMeasurementCard(view) {
  const holder = view.querySelector("#qc-measure-card");
  let sessions;
  try {
    sessions = await apiGet("/api/qc/measurements");
  } catch (e) {
    holder.innerHTML = `<div class="card-title">${icon("check")}测量数据（可靠性闭环）</div>
      <div class="gamut-error" style="margin-top:10px">${esc(e.message)}</div>`;
    return;
  }

  const total = sessions.reduce((acc, s) => {
    const st = s.stats || {};
    acc.v += st.verified || 0; acc.w += st.warning || 0; acc.r += st.rejected || 0;
    return acc;
  }, { v: 0, w: 0, r: 0 });

  holder.innerHTML = `
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <div class="card-title" style="margin:0">${icon("check")}测量数据（可靠性闭环）</div>
      <span style="flex:1"></span>
      <span class="badge badge-green">${total.v} verified</span>
      <span class="badge badge-orange">${total.w} warning</span>
      <span class="badge badge-red">${total.r} rejected</span>
      <button class="btn btn-sm qc-import-btn" type="button">${icon("upload")}导入测量</button>
    </div>
    <p class="swatch-meta" style="margin-top:8px">方法闭环：测前登记 → 校准确认 → 测量 → 复测一致性 → 交叉校验 → 可信度分级。
      只有 <b>verified</b> 数据流向 QC 指标与补偿模型；rejected 数据可查不可用。</p>
    ${sessions.length ? sessions.map((s) => sessionRow(s)).join("") : `
      <div class="empty" style="margin-top:10px">
        <p class="swatch-meta">还没有测量会话。点击「导入测量」登记仪器与校准信息，导入分光/密度计导出的 CGATS.17 或 Status-T 文本。</p>
      </div>`}
  `;

  holder.querySelector(".qc-import-btn").addEventListener("click", () => openImportWizard(view));
  holder.querySelectorAll(".qc-session").forEach((row) => {
    const sid = row.dataset.sid;
    row.querySelector(".qc-session-toggle").addEventListener("click", async () => {
      const box = row.querySelector(".qc-session-records");
      if (!box.hidden) { box.hidden = true; return; }
      try {
        const detail = await apiGet(`/api/qc/measurements?session_id=${encodeURIComponent(sid)}`);
        box.innerHTML = (detail.records || []).map((r) => recordRow(r, sid)).join("")
          || `<p class="swatch-meta" style="padding:8px 0">会话内没有记录</p>`;
        box.hidden = false;
        box.querySelectorAll(".qc-record-del").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const okGo = await confirmDanger({
              title: "删除测量记录",
              message: "删除后整个会话将重新分级（复测配对可能变化）。",
              confirmText: "删除",
            });
            if (!okGo) return;
            try {
              await apiDelete(`/api/qc/measurements?session_id=${encodeURIComponent(btn.dataset.sid)}&record_id=${encodeURIComponent(btn.dataset.rid)}`);
              toast("记录已删除，会话已重分级");
              renderMeasurementCard(view);
            } catch (e) { toast(e.message, "err"); }
          });
        });
      } catch (e) { toast(e.message, "err"); }
    });
  });
}

/* ---- 导入向导：会话登记 → 文件 → 逐文件分级结果 ---- */
async function openImportWizard(view) {
  const [sessions, processes] = await Promise.all([
    apiGet("/api/qc/measurements").catch(() => []),
    apiGet("/api/processes").catch(() => []),
  ]);

  const sessionOptions = ['<option value="">＋ 新建会话（测前登记）</option>']
    .concat(sessions.map((s) => `<option value="${esc(s.id)}">${esc(s.instrument)} · ${esc(s.condition)} · ${esc((s.created_at || "").replace("T", " "))}</option>`))
    .join("");
  const processOptions = ['<option value="">不绑定（跳过纸白比对）</option>']
    .concat(processes.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`))
    .join("");

  const modal = openModal(`
    <h3 class="title">${icon("upload")}导入测量文件</h3>
    <p class="swatch-meta" style="margin-top:4px">支持 CGATS.17（分光/Lab）与 Status-T 密度 TSV 导出，一次可多选；导入后自动按五条规则分级。</p>

    <div class="field" style="margin-top:12px">
      <label>测量会话</label>
      <select id="qc-sess-mode" class="input">${sessionOptions}</select>
    </div>
    <div id="qc-new-fields">
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px">
        <div class="field"><label>仪器 *</label><input id="qc-instrument" class="input" placeholder="如 Eye-One Pro / Xrite530"></div>
        <div class="field"><label>校准时间 *</label><input id="qc-calibrated" class="input" type="datetime-local"></div>
        <div class="field"><label>测量条件</label>
          <select id="qc-condition" class="input"><option>M0</option><option selected>M1</option><option>M2</option></select></div>
        <div class="field"><label>背衬</label>
          <select id="qc-backing" class="input"><option value="">未指定</option><option>黑</option><option>白</option></select></div>
        <div class="field"><label>复测声明</label>
          <select id="qc-repeat" class="input"><option value="single">单次测量</option><option value="repeat">复测组（同布局多次测量）</option></select></div>
        <div class="field"><label>绑定工艺（纸白比对）</label><select id="qc-process" class="input">${processOptions}</select></div>
      </div>
    </div>
    <div id="qc-repeat-fields" hidden style="margin-top:10px">
      <div class="field"><label>复测声明（追加时更新会话）</label>
        <select id="qc-repeat-existing" class="input"><option value="">沿用会话设置</option><option value="single">改为单次测量</option><option value="repeat">改为复测组</option></select></div>
    </div>
    <div class="field" style="margin-top:10px">
      <label>测量文件（.txt，可多选）</label>
      <input id="qc-files" class="input" type="file" accept=".txt,text/plain" multiple>
    </div>

    <div id="qc-results" style="margin-top:12px"></div>
    <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:14px">
      <button class="btn btn-secondary" data-close type="button">关闭</button>
      <button class="btn qc-import-go" type="button">${icon("upload")}开始导入</button>
    </div>
  `);

  const $ = (sel) => modal.modal.querySelector(sel);
  const modeSel = $("#qc-sess-mode");
  modeSel.addEventListener("change", () => {
    $("#qc-new-fields").hidden = modeSel.value !== "";
    $("#qc-repeat-fields").hidden = modeSel.value === "";
  });

  $(".qc-import-go").addEventListener("click", async () => {
    const files = [...$("#qc-files").files];
    if (!files.length) { toast("请先选择测量文件", "err"); return; }
    const useExisting = modeSel.value !== "";
    const params = { filename: "" };
    if (useExisting) {
      params.session_id = modeSel.value;
      params.repeat_mode = $("#qc-repeat-existing").value;
      params.process_id = $("#qc-process").value;
    } else {
      params.instrument = $("#qc-instrument").value.trim();
      params.calibrated_at = $("#qc-calibrated").value;
      params.condition = $("#qc-condition").value;
      params.backing = $("#qc-backing").value;
      params.repeat_mode = $("#qc-repeat").value;
      params.process_id = $("#qc-process").value;
      if (!params.instrument) { toast("请登记仪器（方法闭环第 1 步）", "err"); return; }
      if (!params.calibrated_at) { toast("请确认校准时间（方法闭环第 2 步）", "err"); return; }
    }

    const resultsBox = $("#qc-results");
    const goBtn = $(".qc-import-go");
    goBtn.disabled = true;
    let lastSessionId = params.session_id || "";
    const rows = [];
    for (const f of files) {
      params.filename = f.name;
      const bytes = new Uint8Array(await f.arrayBuffer());
      try {
        const res = await apiPostRaw("/api/qc/measurements/import", params, bytes);
        lastSessionId = res.session?.id || lastSessionId;
        const r = res.record || {};
        const reasons = (r.reasons || []).slice(0, 2).map(esc);
        rows.push(`
          <div style="display:flex; align-items:flex-start; gap:10px; padding:8px 0; border-top:1px solid var(--separator)">
            ${reliabilityBadge(r.reliability || "warning")}
            <div style="flex:1; min-width:0">
              <b style="font-size:13px">${esc(f.name)}</b>
              <span class="swatch-meta">${r.patch_count ?? "?"} 块</span>
              ${reasons.length ? `<div class="swatch-meta" style="margin-top:2px">${reasons.join("；")}</div>` : ""}
            </div>
          </div>`);
      } catch (e) {
        rows.push(`
          <div style="display:flex; gap:10px; padding:8px 0; border-top:1px solid var(--separator)">
            <span class="badge badge-red">${icon("xmark")}失败</span>
            <div class="swatch-meta">${esc(f.name)} — ${esc(e.message)}</div>
          </div>`);
      }
      resultsBox.innerHTML = rows.join("");
    }
    goBtn.disabled = false;
    const sess = sessions.find((s) => s.id === lastSessionId) || { instrument: "新会话" };
    toast(`导入完成（会话 ${sess.instrument}）`);
    renderMeasurementCard(view);
  });

  modal.modal.querySelector("[data-close]").addEventListener("click", () => modal.close());
}

/* 主趋势图：时间序列 ΔE 折线 + 容差带（SVG，主题令牌配色） */
function trendChart(series, tol) {
  const W = 720;
  const H = 240;
  const PAD = { l: 44, r: 14, t: 14, b: 26 };
  if (!series.length) {
    return `<div class="gamut-error">暂无配方记录</div>`;
  }
  const des = series.map((p) => p.delta_e);
  // y 上限：P95×1.5 与 5 取大、封顶 12（超差点夹到顶并空心标记）
  const p95 = [...des].sort((a, b) => a - b)[Math.floor(des.length * 0.95)] || 2;
  const yMax = Math.min(12, Math.max(2.5, p95 * 1.5));
  const x = (i) => PAD.l + (i / Math.max(series.length - 1, 1)) * (W - PAD.l - PAD.r);
  const y = (v) => PAD.t + (1 - Math.min(v, yMax) / yMax) * (H - PAD.t - PAD.b);
  const colorFor = (v) =>
    v <= tol ? "var(--green)" : v <= 5 ? "var(--orange)" : "var(--red)";

  // 折线（超差夹顶的点仍连线，但标记为空心）
  const path = series
    .map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.delta_e).toFixed(1)}`)
    .join(" ");
  const dots = series
    .map(
      (p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(p.delta_e).toFixed(1)}" r="3.2"
        fill="${p.delta_e > yMax ? "none" : colorFor(p.delta_e)}"
        stroke="${p.delta_e > yMax ? colorFor(p.delta_e) : "none"}" stroke-width="1.4"
        opacity="0.92"><title>${esc(p.created_at.replace("T", " "))} · ${esc(p.process_name || "-")}
        ${esc(p.label || "")} · ΔE ${p.delta_e}${p.delta_e > yMax ? "（超出图示范围）" : ""}</title></circle>`
    )
    .join("");

  const yTicks = [0, yMax / 2, yMax].map(
    (v) => `<text x="${PAD.l - 8}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--label-3)">${v.toFixed(1)}</text>
      <line x1="${PAD.l}" y1="${y(v)}" x2="${W - PAD.r}" y2="${y(v)}" stroke="var(--separator)" stroke-width="0.6"/>`
  ).join("");

  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%; height:auto; display:block" role="img" aria-label="ΔE 趋势图">
    ${yTicks}
    <line x1="${PAD.l}" y1="${y(tol)}" x2="${W - PAD.r}" y2="${y(tol)}" stroke="var(--green)" stroke-width="1" stroke-dasharray="5 4" opacity="0.75"/>
    <text x="${W - PAD.r}" y="${y(tol) - 5}" text-anchor="end" font-size="10" fill="var(--green)">容差 ${tol}</text>
    <line x1="${PAD.l}" y1="${y(5)}" x2="${W - PAD.r}" y2="${y(5)}" stroke="var(--red)" stroke-width="1" stroke-dasharray="5 4" opacity="0.6"/>
    <text x="${W - PAD.r}" y="${y(5) - 5}" text-anchor="end" font-size="10" fill="var(--red)">ΔE 5</text>
    <path d="${path}" fill="none" stroke="var(--label-3)" stroke-width="1.6" opacity="0.8"/>
    ${dots}
  </svg>`;
}

/* 分工艺行：n / mean / P95 / 达标率 / 迷你趋势 / 漂移 chip */
function processRow(p, tol) {
  const driftChip = p.drift_flag
    ? `<span class="chip" style="background:var(--red-soft); color:var(--red)">漂移 ↑${p.drift}</span>`
    : p.drift != null
      ? `<span class="chip chip-mono">Δ ${p.drift > 0 ? "+" : ""}${p.drift}</span>`
      : "";
  return `
    <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:12px 0; border-top:1px solid var(--separator)">
      <a href="#/processes/${p.process_id}" style="font-weight:600; color:var(--label); min-width:140px">${esc(p.process_name || "未命名工艺")}</a>
      <span class="chip chip-mono">n=${p.n}</span>
      <span class="swatch-meta">平均 ΔE <b>${p.mean_de ?? "-"}</b> · P95 <b>${p.p95_de ?? "-"}</b></span>
      ${p.pass_rate != null ? `<span class="badge ${p.pass_rate >= 0.9 ? "badge-green" : p.pass_rate >= 0.7 ? "badge-orange" : "badge-red"}">达标 ${(p.pass_rate * 100).toFixed(0)}%</span>` : ""}
      ${driftChip}
      <span style="flex:1"></span>
      <a class="btn btn-secondary btn-sm" href="#/recipes?process=${p.process_id}">${icon("wand")}预测</a>
    </div>`;
}

export async function renderMonitorView(view) {
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">监控</h1>
        <p class="page-sub">配方 ΔE 趋势、工艺漂移告警与测量数据可靠性闭环</p>
      </div>
    </div>
    <div id="monitor-body">${skeletonCards(4)}</div>
    <div id="qc-measure-card" class="card" style="margin-top:14px">${skeletonCards(1)}</div>
  `;
  // 测量数据卡独立加载（与配方统计互不阻塞）
  renderMeasurementCard(view);

  let stats;
  try {
    stats = await apiGet("/api/recipes/stats");
  } catch (e) {
    view.querySelector("#monitor-body").innerHTML = errorState(e.message);
    const retry = view.querySelector(".btn-retry");
    if (retry) retry.addEventListener("click", () => renderMonitorView(view));
    return;
  }

  if (!stats.total) {
    view.querySelector("#monitor-body").innerHTML = `
      ${emptyState({
        iconName: "chart",
        title: "还没有可监控的数据",
        desc: "完成一次配方预测后，这里会出现 ΔE 趋势、达标率与漂移告警。",
        action: `<a class="btn" href="#/recipes">${icon("wand")}去预测</a>`,
      })}`;
    return;
  }

  const tol = TOL_DEFAULT;
  const body = view.querySelector("#monitor-body");
  body.innerHTML = `
    <div class="stat-grid">
      <div class="card stat"><div class="stat-val">${stats.total}</div><div class="stat-label">配方总数</div></div>
      <div class="card stat"><div class="stat-val">${stats.converged_rate != null ? (stats.converged_rate * 100).toFixed(0) + "%" : "-"}</div><div class="stat-label">达标率（ΔE≤容差）</div></div>
      <div class="card stat"><div class="stat-val">${stats.mean_de ?? "-"}</div><div class="stat-label">平均 ΔE</div></div>
      <div class="card stat"><div class="stat-val">${stats.p95_de ?? "-"}</div><div class="stat-label">P95 ΔE</div></div>
    </div>

    ${stats.flags.length ? `
    <div class="card" style="margin-top:14px">
      <div class="card-title">${icon("warn")}漂移告警</div>
      ${stats.flags.map((f) => `
        <div class="issue issue-warn" style="margin-top:10px">${icon("warn")}
          <span><b>${esc(f.process_name)}</b> — ${esc(f.message)}</span>
        </div>`).join("")}
    </div>` : ""}

    <div class="card" style="margin-top:14px">
      <div class="card-title">${icon("chart")}ΔE 趋势（全部工艺）</div>
      <div style="margin-top:10px">${trendChart(stats.series, tol)}</div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="card-title">${icon("stack")}分工艺统计</div>
      <div style="margin-top:4px">${stats.processes.map((p) => processRow(p, tol)).join("")}</div>
    </div>
  `;
}
