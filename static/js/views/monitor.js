/* 监控视图（V0.4/V0.5）：ΔE 趋势总览 + 分工艺漂移告警 + 测量数据可靠性闭环。
 * 数据源：GET /api/recipes/stats + GET /api/qc/measurements。
 * V0.5 测量数据卡：会话登记（仪器/条件/校准/背衬/复测模式）→ 导入 → 五条规则分级
 * （verified/warning/rejected），rejected 数据可查不可用。
 */

import { apiGet, apiPostRaw, apiDelete } from "../api.js";
import { icon } from "../icons.js";
import {
  emptyState, errorState, skeletonCards, esc, openModal, toast, confirmDanger, observeRedraw,
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

/* =========================================================
 * V0.5 印刷 QC 卡：GATF 四指标 + TVI 曲线 + GATF 色轮
 * 只消费 verified 数据；无 verified 时显示方法要求占位（教育用户何为可信数据）
 * ========================================================= */

const INK_COLORS = { C: "#1f7fd0", M: "#d04f9e", Y: "#c99a1e" };

/* GATF 色轮理想方位（自顶部顺时针，Y/R/M/B/C/G 每 60°） */
const WHEEL_ANGLE = { Y: 0, R: 60, M: 120, B: 180, C: 240, G: 300 };
/* 色相偏移方向：中间滤色对应的相邻色方位（+1 顺时针 / -1 逆时针） */
const SHIFT_SIGN = { Y_M: 1, Y_C: -1, M_Y: -1, M_C: 1, C_M: -1, C_Y: 1 };

function midFilter(dens) {
  const v = { C: dens.C, M: dens.M, Y: dens.Y };
  return Object.keys(v).sort((a, b) => v[a] - v[b])[1];
}

function setupCanvas(canvas, w, h) {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = "100%";
  canvas.style.maxWidth = `${w}px`;
  canvas.style.height = "auto";
  canvas.style.aspectRatio = `${w} / ${h}`;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return ctx;
}

/* TVI 曲线：名义网点率 vs 网点增大（pp），理想线 y=0 */
function drawTvi(canvas, points) {
  const W = 460, H = 240, PAD = { l: 40, r: 14, t: 16, b: 28 };
  const ctx = setupCanvas(canvas, W, H);
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--label-3").trim() || "#999";
  const sep = css.getPropertyValue("--separator").trim() || "#ddd";
  ctx.clearRect(0, 0, W, H);

  if (!points.length) {
    ctx.fillStyle = ink;
    ctx.font = "12px system-ui";
    ctx.fillText("verified 数据中没有 c/m/y 网点梯尺块（标签形如 c60），无法计算 TVI", PAD.l, H / 2);
    return;
  }
  const xs = points.map((p) => p.nominal);
  const ys = points.map((p) => p.tvi * 100);
  const xMax = Math.max(100, ...xs);
  const yMin = Math.min(-2, ...ys) - 1;
  const yMax = Math.max(4, ...ys) + 2;
  const X = (v) => PAD.l + (v / xMax) * (W - PAD.l - PAD.r);
  const Y = (v) => PAD.t + (1 - (v - yMin) / (yMax - yMin)) * (H - PAD.t - PAD.b);

  // 网格与轴
  ctx.strokeStyle = sep; ctx.lineWidth = 0.8; ctx.font = "10px system-ui";
  for (const g of [0, 25, 50, 75, 100]) {
    if (g > xMax) break;
    ctx.beginPath(); ctx.moveTo(X(g), PAD.t); ctx.lineTo(X(g), H - PAD.b); ctx.stroke();
    ctx.fillStyle = ink;
    ctx.fillText(`${g}%`, X(g) - 8, H - PAD.b + 14);
  }
  for (const g of [yMin, 0, yMax]) {
    ctx.beginPath(); ctx.moveTo(PAD.l, Y(g)); ctx.lineTo(W - PAD.r, Y(g)); ctx.stroke();
    ctx.fillStyle = ink;
    ctx.fillText(`${g > 0 ? "+" : ""}${g.toFixed(0)}`, 8, Y(g) + 3);
  }
  // 理想 TVI=0 线
  ctx.strokeStyle = "rgba(127,127,127,0.6)"; ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(PAD.l, Y(0)); ctx.lineTo(W - PAD.r, Y(0)); ctx.stroke();
  ctx.setLineDash([]);

  // 每色折线
  for (const inkKey of ["C", "M", "Y"]) {
    const pts = points.filter((p) => p.ink === inkKey).sort((a, b) => a.nominal - b.nominal);
    if (!pts.length) continue;
    ctx.strokeStyle = INK_COLORS[inkKey]; ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach((p, i) => {
      const px = X(p.nominal), py = Y(p.tvi * 100);
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    ctx.stroke();
    ctx.fillStyle = INK_COLORS[inkKey];
    for (const p of pts) {
      ctx.beginPath();
      ctx.arc(X(p.nominal), Y(p.tvi * 100), 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.fillStyle = ink;
  ctx.font = "11px system-ui";
  ctx.fillText("TVI = 实测网点面积 − 名义网点率（Murray–Davies）", PAD.l + 4, PAD.t + 12);
}

/* GATF 色轮：六方位（Y/R/M/B/C/G），色相误差旋转 + 灰度收缩半径 */
function drawWheel(canvas, patches) {
  const S = 300;
  const ctx = setupCanvas(canvas, S, S);
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--label-3").trim() || "#999";
  const sep = css.getPropertyValue("--separator").trim() || "#ddd";
  ctx.clearRect(0, 0, S, S);
  const cx = S / 2, cy = S / 2, R = S / 2 - 34;

  // 底圆 + 六方位辐条
  ctx.strokeStyle = sep; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, cy, R * 0.5, 0, Math.PI * 2); ctx.stroke();
  ctx.font = "12px system-ui"; ctx.textAlign = "center";
  for (const [label, deg] of Object.entries(WHEEL_ANGLE)) {
    const rad = ((90 - deg) * Math.PI) / 180;
    const px = cx + Math.cos(rad) * R, py = cy - Math.sin(rad) * R;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(px, py); ctx.stroke();
    ctx.fillStyle = INK_COLORS[label] || ink;
    ctx.fillText(label, cx + Math.cos(rad) * (R + 16), cy - Math.sin(rad) * (R + 16) + 4);
  }

  // 数据点：灰度→半径收缩（灰度低≈纯色→靠外），色相误差→绕理想方位旋转
  for (const p of patches) {
    const deg0 = WHEEL_ANGLE[p.label.toUpperCase()];
    if (deg0 == null) continue;
    const sign = SHIFT_SIGN[`${p.label.toUpperCase()}_${midFilter(p.densities)}`] || 0;
    const deg = deg0 + sign * p.hue_error * 60;
    const rad = ((90 - deg) * Math.PI) / 180;
    const rr = Math.max(0.08, 1 - p.grayness) * R;
    const px = cx + Math.cos(rad) * rr, py = cy - Math.sin(rad) * rr;
    ctx.fillStyle = INK_COLORS[p.label.toUpperCase()] || ink;
    ctx.beginPath(); ctx.arc(px, py, 6, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,0.25)"; ctx.lineWidth = 1; ctx.stroke();
  }
  ctx.fillStyle = ink;
  ctx.font = "11px system-ui"; ctx.textAlign = "left";
  ctx.fillText("半径 = 1 − 灰度 · 角偏移 = 色相误差 × 60°", 8, S - 8);
}

function inkCard(label, m) {
  const pct = (v) => `${(v * 100).toFixed(1)}%`;
  return `
    <div style="flex:1; min-width:150px; border:1px solid var(--separator); border-radius:10px; padding:10px 12px">
      <div style="display:flex; align-items:center; gap:8px">
        <span style="width:12px; height:12px; border-radius:4px; background:${INK_COLORS[label]}"></span>
        <b style="color:var(--label)">${esc(m.label)}</b>
        <span class="chip chip-mono">DH ${m.strength.toFixed(2)}</span>
      </div>
      <div class="swatch-meta" style="margin-top:6px; line-height:1.9">
        色相误差 <b>${pct(m.hue_error)}</b> · 灰度 <b>${pct(m.grayness)}</b><br>
        色效率 <b>${pct(m.efficiency)}</b> · 色纯度 <b>${pct(m.purity)}</b><br>
        滤色密度 C/M/Y <b>${m.densities.C.toFixed(2)} / ${m.densities.M.toFixed(2)} / ${m.densities.Y.toFixed(2)}</b>
      </div>
    </div>`;
}

async function renderQcCard(view) {
  const holder = view.querySelector("#qc-ink-card");
  let sessions;
  try {
    sessions = await apiGet("/api/qc/measurements");
  } catch (e) {
    holder.innerHTML = `<div class="card-title">${icon("printer")}印刷 QC（GATF）</div>
      <div class="gamut-error" style="margin-top:10px">${esc(e.message)}</div>`;
    return;
  }
  if (!sessions.length) {
    holder.innerHTML = `
      <div class="card-title">${icon("printer")}印刷 QC（GATF）</div>
      <div class="empty" style="margin-top:10px">
        <p class="swatch-meta">还没有测量会话。先在上方「测量数据」导入 Status-T 密度导出（含实地与网点梯尺块），
        这里会自动给出色强度 / 色相误差 / 灰度 / 色效率、TVI 曲线与 GATF 色轮。</p>
      </div>`;
    return;
  }

  const options = sessions.map((s, i) =>
    `<option value="${esc(s.id)}">${esc(s.instrument)} · ${esc(s.condition)} · ${esc((s.created_at || "").replace("T", " "))}</option>`
  ).join("");

  holder.innerHTML = `
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <div class="card-title" style="margin:0">${icon("printer")}印刷 QC（GATF）</div>
      <span class="chip">仅消费 verified</span>
      <span style="flex:1"></span>
      <select id="qc-ink-session" class="input" style="max-width:280px">${options}</select>
    </div>
    <div id="qc-ink-body" style="margin-top:12px">${skeletonCards(1)}</div>
  `;

  const load = async () => {
    const sid = holder.querySelector("#qc-ink-session").value;
    const body = holder.querySelector("#qc-ink-body");
    let data;
    try {
      data = await apiGet(`/api/qc/ink-quality?session_id=${encodeURIComponent(sid)}`);
    } catch (e) {
      body.innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
      return;
    }

    if (!data.verified_records || !Object.keys(data.inks).length) {
      body.innerHTML = `
        <div class="empty">
          <div class="empty-title">本会话暂无可信（verified）测量数据</div>
          <p class="swatch-meta" style="margin-top:6px; text-align:left; line-height:2">
            QC 指标只消费通过方法闭环的数据，要求：<br>
            ① 复测声明开启且同标签复测 ΔE ≤ 0.4（每个目标至少测 2 次）；<br>
            ② 纸白块正常（未漂移、背衬正确）；<br>
            ③ Lab↔XYZ 交叉校验一致、密度在物理量程内（无 warning/rejected）。
          </p>
          <p class="swatch-meta">当前：${data.verified_records} 条 verified · ${data.skipped_records} 条被排除（${(data.source_records || []).join("、") || "无来源"}）</p>
        </div>`;
      return;
    }

    const inkKeys = ["C", "M", "Y"].filter((k) => data.inks[k]);
    const wheelPatches = [...inkKeys.map((k) => ({ label: k, ...data.inks[k] })),
      ...data.overprints.filter((o) => WHEEL_ANGLE[o.label.toUpperCase()] != null)];
    body.innerHTML = `
      <div style="display:flex; gap:10px; flex-wrap:wrap">${inkKeys.map((k) => inkCard(k, data.inks[k])).join("")}</div>
      <div style="display:flex; gap:14px; flex-wrap:wrap; margin-top:14px">
        <div style="flex:1; min-width:320px">
          <div class="card-title" style="font-size:14px">TVI 网点增大曲线</div>
          <canvas id="qc-tvi-canvas" style="margin-top:8px"></canvas>
        </div>
        <div style="min-width:280px">
          <div class="card-title" style="font-size:14px">GATF 色轮</div>
          <canvas id="qc-wheel-canvas" style="margin-top:8px"></canvas>
        </div>
      </div>
      ${data.skipped_records ? `<p class="swatch-meta" style="margin-top:10px">已排除 ${data.skipped_records} 条未达 verified 的记录（warning/rejected）——QC 指标不消费未过门禁的数据。</p>` : ""}
    `;

    const tviCanvas = body.querySelector("#qc-tvi-canvas");
    const wheelCanvas = body.querySelector("#qc-wheel-canvas");
    drawTvi(tviCanvas, data.tvi);
    drawWheel(wheelCanvas, wheelPatches);
    observeRedraw(body, () => {
      drawTvi(tviCanvas, data.tvi);
      drawWheel(wheelCanvas, wheelPatches);
    });
  };

  holder.querySelector("#qc-ink-session").addEventListener("change", load);
  await load();
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
    <div id="qc-ink-card" class="card" style="margin-top:14px">${skeletonCards(1)}</div>
  `;
  // 测量数据卡与 QC 卡独立加载（与配方统计互不阻塞）
  renderMeasurementCard(view);
  renderQcCard(view);

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
