/* 监控视图（V0.4）：ΔE 趋势总览 + 分工艺漂移告警 + 分段统计。
 * 数据源：GET /api/recipes/stats（时间序列 + 分工艺聚合 + 漂移 flag）。
 * 图表：SVG 折线（复用 sparkline 的阈值配色模式），点色 = statusBadge 阈值色。
 */

import { apiGet } from "../api.js";
import { icon } from "../icons.js";
import {
  emptyState, errorState, skeletonCards, esc,
} from "../components.js";

const TOL_DEFAULT = 2.0;

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
        <p class="page-sub">配方 ΔE 趋势与工艺漂移告警 —— 数据来自配方实测与预测历史</p>
      </div>
    </div>
    <div id="monitor-body">${skeletonCards(4)}</div>
  `;

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
