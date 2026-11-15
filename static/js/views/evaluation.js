/* 主观评价卡（V0.6 · M4）：GB/T 34053 方法论 —— 标准观测环境下多评价人独立
 * 5 级打分（1 非常差 … 5 非常好），每指标统计算术平均与标准差；标准差反映打分
 * 一致性，离散过大提示评价尺度失稳。三套模板按材质差异化（书刊/纸包装/塑料包装）。
 * 数据源：GET/POST/DELETE /api/qc/evaluations*。
 */

import { apiGet, apiPost, apiDelete } from "../api.js";
import { icon } from "../icons.js";
import { esc, openModal, toast, confirmDanger } from "../components.js";

let _templates = null;
async function getTemplates() {
  if (!_templates) {
    _templates = await apiGet("/api/qc/evaluations/templates");
  }
  return _templates;
}

function overallBadge(overall) {
  if (overall == null) return `<span class="badge badge-gray">无打分</span>`;
  const cls = overall >= 4.5 ? "badge-green" : overall >= 4.0 ? "badge-blue"
    : overall >= 3.0 ? "badge-orange" : "badge-red";
  return `<span class="badge ${cls}">综合 ${overall.toFixed(2)}</span>`;
}

function evalRow(e) {
  const st = e.stats || {};
  const warn = (st.consistency_warn || []).length;
  return `
    <div class="qc-eval-item" data-eid="${esc(e.id)}" data-template="${esc(e.template)}"
      style="border-top:1px solid var(--separator); padding:10px 0">
      <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
        <b style="color:var(--label)">${esc(e.title)}</b>
        <span class="chip">${esc(e.template_name || "")}</span>
        ${overallBadge(st.overall)}
        <span class="chip chip-mono">${st.entries || 0} 位评价人</span>
        ${warn ? `<span class="badge badge-orange">打分分歧 ${warn} 项</span>` : ""}
        <span style="flex:1"></span>
        <button class="btn btn-secondary btn-sm qc-eval-score" type="button">${icon("pencil")}录入打分</button>
        <button class="btn btn-secondary btn-sm qc-eval-toggle" type="button">${icon("doc")}统计明细</button>
        <button class="btn btn-ghost btn-sm qc-eval-del" type="button" title="删除评价">${icon("trash")}</button>
      </div>
      <div class="qc-eval-detail" hidden style="margin-top:8px"></div>
    </div>`;
}

/* 统计明细：每指标 n/均值/标准差（分歧行高亮）+ 综合分 */
function statsHtml(stats) {
  const rows = (stats.rows || []).map((r) => {
    const warn = r.std != null && r.std > (stats.std_warn ?? 0.8);
    return `<tr ${warn ? 'style="background:var(--orange-soft, rgba(255,170,0,0.08))"' : ""}>
      <td style="padding:5px 10px 5px 0">${esc(r.item)}</td>
      <td class="chip-mono" style="padding:5px 10px 5px 0">n=${r.n}</td>
      <td style="padding:5px 10px 5px 0"><b>${r.mean == null ? "-" : r.mean.toFixed(2)}</b></td>
      <td style="padding:5px 0">${r.std == null ? `<span class="swatch-meta">需≥2人</span>`
        : `<b>${r.std.toFixed(2)}</b> ${warn ? `<span class="badge badge-orange">分歧</span>` : ""}`}</td>
    </tr>`;
  }).join("");
  return `
    <table style="width:100%; font-size:12px; border-collapse:collapse">
      <tr class="swatch-meta"><td style="padding:3px 10px 3px 0">指标</td><td style="padding:3px 10px 3px 0">样本</td>
        <td style="padding:3px 10px 3px 0">算术平均</td><td style="padding:3px 0">标准差（一致性）</td></tr>
      ${rows}
    </table>
    <p class="swatch-meta" style="margin-top:6px">综合平均得分 <b>${stats.overall ?? "-"}</b>
      · ${(stats.consistency_warn || []).length
        ? `一致性预警：${stats.consistency_warn.map(esc).join("、")}（σ>${stats.std_warn ?? 0.8}，建议重新统一打分尺度）`
        : "打分一致性良好"}</p>`;
}

/* 创建评价 */
async function openCreateModal(view, holder) {
  let templates;
  try {
    templates = await getTemplates();
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  let processes = [];
  try {
    processes = await apiGet("/api/processes");
  } catch { /* 不绑定也可创建 */ }
  const tplOptions = Object.entries(templates.templates || {})
    .map(([key, t]) => {
      const count = (t.groups || []).reduce((n, g) => n + g.items.length, 0);
      return `<option value="${esc(key)}">${esc(t.name)}（${count} 项指标）</option>`;
    })
    .join("");
  const procOptions = ['<option value="">不绑定工艺</option>']
    .concat(processes.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`))
    .join("");

  const { modal, close } = openModal(`
    <h3 class="modal-title">创建主观评价</h3>
    <div style="display:flex; flex-direction:column; gap:10px; margin-top:12px">
      <label class="field"><span class="field-label">评价模板</span>
        <select id="ev-tpl" class="input">${tplOptions}</select></label>
      <label class="field"><span class="field-label">标题（留空用模板名）</span>
        <input id="ev-title" class="input" placeholder="如：季度包装抽检评价"></label>
      <label class="field"><span class="field-label">绑定工艺（可选）</span>
        <select id="ev-proc" class="input">${procOptions}</select></label>
      <p class="swatch-meta">创建后逐位评价人录入 5 级打分（1 非常差 … 5 非常好），
        每位评价人独立打分互不干扰；回收 ≥2 份后统计均值与标准差。</p>
      <div style="display:flex; gap:10px; justify-content:flex-end">
        <button class="btn btn-secondary" id="ev-cancel" type="button">取消</button>
        <button class="btn" id="ev-save" type="button">${icon("check")}创建</button>
      </div>
    </div>
  `);
  modal.querySelector("#ev-cancel").addEventListener("click", close);
  modal.querySelector("#ev-save").addEventListener("click", async () => {
    const template = modal.querySelector("#ev-tpl").value;
    const title = modal.querySelector("#ev-title").value.trim();
    const processId = modal.querySelector("#ev-proc").value;
    try {
      await apiPost(`/api/qc/evaluations?template=${encodeURIComponent(template)}&title=${encodeURIComponent(title)}&process_id=${encodeURIComponent(processId)}`, {});
      toast("评价已创建，开始录入打分");
      close();
      renderEvalCardInto(view, holder);
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* 录入打分：每指标一行 1-5 按钮组（单选），提交为该评价人完整打分 */
async function openScoreModal(view, holder, eid, template) {
  let templates;
  try {
    templates = await getTemplates();
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  const tpl = (templates.templates || {})[template];
  if (!tpl) {
    toast("模板缺失", "err");
    return;
  }
  const scale = templates.score_scale || {};
  const groups = (tpl.groups || []).map((g) => `
    <div style="margin-top:12px">
      <div class="card-title" style="font-size:13px">${esc(g.group)}</div>
      ${g.items.map((item, idx) => `
        <div class="ev-score-row" data-item="${esc(item)}" style="display:flex; align-items:center; gap:8px; padding:7px 0; border-top:1px solid var(--separator)">
          <span style="flex:1; min-width:180px; font-size:13px; color:var(--label)">${esc(item)}</span>
          <div style="display:flex; gap:4px">
            ${[1, 2, 3, 4, 5].map((v) => `
              <button type="button" class="btn btn-secondary btn-sm ev-vote" data-v="${v}"
                title="${esc(scale[String(v)] || v)}" style="min-width:32px; padding:4px 0">${v}</button>`).join("")}
          </div>
        </div>`).join("")}
    </div>`).join("");

  const { modal, close } = openModal(`
    <h3 class="modal-title">录入打分 · ${esc(tpl.name)}</h3>
    <div style="margin-top:10px">
      <label class="field"><span class="field-label">评价人（独立打分，互不干扰）</span>
        <input id="ev-name" class="input" placeholder="姓名或编号"></label>
      <div style="max-height:46vh; overflow:auto; margin-top:8px">${groups}</div>
      <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:14px">
        <button class="btn btn-secondary" id="ev-cancel" type="button">取消</button>
        <button class="btn" id="ev-submit" type="button">${icon("check")}提交打分</button>
      </div>
    </div>
  `);

  modal.querySelectorAll(".ev-score-row").forEach((row) => {
    row.querySelectorAll(".ev-vote").forEach((btn) => {
      btn.addEventListener("click", () => {
        row.dataset.value = btn.dataset.v;
        row.querySelectorAll(".ev-vote").forEach((b) => {
          b.style.background = "";
          b.style.color = "";
        });
        btn.style.background = "var(--accent, var(--brand, #2c6e49))";
        btn.style.color = "#fff";
      });
    });
  });

  modal.querySelector("#ev-cancel").addEventListener("click", close);
  modal.querySelector("#ev-submit").addEventListener("click", async () => {
    const evaluator = modal.querySelector("#ev-name").value.trim();
    if (!evaluator) {
      toast("请填写评价人", "err");
      return;
    }
    const scores = {};
    let missing = 0;
    modal.querySelectorAll(".ev-score-row").forEach((row) => {
      if (row.dataset.value) scores[row.dataset.item] = Number(row.dataset.value);
      else missing += 1;
    });
    if (missing) {
      toast(`还有 ${missing} 项未打分`, "err");
      return;
    }
    try {
      await apiPost(`/api/qc/evaluations/entries?evaluation_id=${encodeURIComponent(eid)}&evaluator=${encodeURIComponent(evaluator)}`, scores);
      toast("打分已录入");
      close();
      renderEvalCardInto(view, holder);
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

async function renderEvalCardInto(view, holder) {
  let evals;
  try {
    evals = await apiGet("/api/qc/evaluations");
  } catch (e) {
    holder.innerHTML = `<div class="card-title">${icon("compare")}主观评价</div>
      <div class="gamut-error" style="margin-top:10px">${esc(e.message)}</div>`;
    return;
  }

  holder.innerHTML = `
    <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <div class="card-title" style="margin:0">${icon("compare")}主观评价（GB/T 34053 方法论）</div>
      <span style="flex:1"></span>
      <button class="btn btn-sm qc-eval-create" type="button">${icon("plus")}创建评价</button>
    </div>
    <p class="swatch-meta" style="margin-top:8px">标准观测环境（D65 光源、无杂光、规定视距）下多名评价人独立 5 级打分，
      每指标统计算术平均与标准差——标准差反映打分一致性，离散过大需重新统一尺度。</p>
    ${evals.length ? evals.map(evalRow).join("") : `
      <div class="empty" style="margin-top:10px">
        <p class="swatch-meta">还没有主观评价。创建一个检查单（书刊 / 纸包装 / 塑料包装模板），
        为每位评价人录入打分，系统自动统计均值、标准差与一致性。</p>
      </div>`}
  `;

  holder.querySelector(".qc-eval-create").addEventListener("click", () => openCreateModal(view, holder));
  holder.querySelectorAll(".qc-eval-item").forEach((row) => {
    const eid = row.dataset.eid;
    const template = row.dataset.template;
    row.querySelector(".qc-eval-score").addEventListener("click", () => openScoreModal(view, holder, eid, template));
    row.querySelector(".qc-eval-toggle").addEventListener("click", async () => {
      const box = row.querySelector(".qc-eval-detail");
      if (!box.hidden) {
        box.hidden = true;
        return;
      }
      if (!box.innerHTML) {
        try {
          const detail = await apiGet(`/api/qc/evaluations?evaluation_id=${encodeURIComponent(eid)}`);
          box.innerHTML = statsHtml(detail.stats || {});
        } catch (e) {
          box.innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
        }
      }
      box.hidden = false;
    });
    row.querySelector(".qc-eval-del").addEventListener("click", async () => {
      const okGo = await confirmDanger({
        title: "删除主观评价",
        message: "将删除该评价的全部打分记录与统计，不可恢复。",
        confirmText: "删除",
      });
      if (!okGo) return;
      try {
        await apiDelete(`/api/qc/evaluations?evaluation_id=${encodeURIComponent(eid)}`);
        toast("评价已删除");
        renderEvalCardInto(view, holder);
      } catch (e) {
        toast(e.message, "err");
      }
    });
  });
}

export async function renderEvalCard(view) {
  const holder = view.querySelector("#qc-eval-card");
  await renderEvalCardInto(view, holder);
}
