/* 工艺库视图：工艺卡片列表 + 新建（上传 ICC）+ 详情（色域/元数据/配方历史） */

import { apiGet, apiDelete, apiUploadICC, fileToArrayBuffer } from "../api.js";
import { icon } from "../icons.js";
import {
  toast, emptyState, sparkline, drawCie, openModal, recipeLine, fetchSpectrum,
} from "../components.js";
import { updateNavCount } from "./shell.js";

const SRGB_GAMUT = [
  [0.64, 0.33], [0.30, 0.60], [0.15, 0.06],
];

export async function renderProcessesView(view) {
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">工艺库</h1>
        <p class="page-sub">工艺 = 打印机 + 纸张 + 墨水的组合，每个工艺绑定自己的 ICC 特性文件</p>
      </div>
      <button class="btn" id="btn-new">${icon("plus")}新建工艺</button>
    </div>
    <div id="process-list"><div class="spinner"></div></div>
  `;
  view.querySelector("#btn-new").addEventListener("click", () => openNewProcessModal(view));
  await loadList(view);
}

async function loadList(view) {
  const box = view.querySelector("#process-list");
  try {
    const list = await apiGet("/api/processes");
    updateNavCount();
    if (!list.length) {
      box.innerHTML = emptyState({
        iconName: "stack", title: "还没有工艺",
        desc: "上传一个 ICC 特性文件即可创建工艺。可以用打印机厂商提供的、RIP 导出的或系统自带的 ICC 文件。",
        action: `<button class="btn" id="empty-new">${icon("plus")}新建工艺</button>`,
      });
      box.querySelector("#empty-new").addEventListener("click", () => openNewProcessModal(view));
      return;
    }
    box.innerHTML = `<div class="card-grid">${list.map(cardHtml).join("")}</div>`;
    box.querySelectorAll(".process-card").forEach((el) => {
      el.addEventListener("click", () => (location.hash = `#/processes/${el.dataset.id}`));
    });
    // 迷你色域图
    for (const el of box.querySelectorAll(".process-card")) {
      const mini = el.querySelector(".process-mini-cie");
      try {
        const g = await apiGet(`/api/processes/${el.dataset.id}/gamut?levels=4`);
        const canvas = document.createElement("canvas");
        canvas.className = "process-mini-cie";
        canvas.style.width = "44px";
        canvas.style.height = "44px";
        mini.replaceWith(canvas);
        drawCie(canvas, [{ points: g.xy_points, color: "var(--accent)" }]);
      } catch (_) {
        /* 色域失败保持占位 */
      }
    }
  } catch (e) {
    box.innerHTML = `<div class="card"><div class="issue issue-error">${icon("xmark")}<span>${esc(e.message)}</span></div></div>`;
  }
}

function cardHtml(p) {
  const space = p.icc_meta.device_space;
  return `
  <div class="card process-card" data-id="${p.id}">
    <div class="process-head">
      <div class="process-mini-cie"></div>
      <div style="min-width:0">
        <div style="font-weight:700; font-size:15px" class="truncate">${esc(p.name)}</div>
        <div style="font-size:12px;color:var(--label-2);margin-top:2px">${esc(p.icc_meta.description || p.icc_original_name)}</div>
      </div>
    </div>
    <div style="display:flex; gap:6px; flex-wrap:wrap">
      <span class="badge ${space === "CMYK" ? "badge-blue" : "badge-gray"}">${space} · ${p.icc_meta.channels}通道</span>
      ${p.paper ? `<span class="chip">${esc(p.paper)}</span>` : ""}
      ${p.ink ? `<span class="chip">${esc(p.ink)}</span>` : ""}
    </div>
  </div>`;
}

/* ---------- 新建工艺 ---------- */
function openNewProcessModal(view) {
  const m = openModal(`
    <h3 class="card-title">${icon("upload")}新建工艺</h3>
    <p class="card-desc">上传该工艺的 ICC 特性文件（.icc / .icm），文件将保存在本机 data/processes/ 目录。</p>
    <div class="field-row">
      <div class="field"><label>工艺名称 *</label><input class="input" id="np-name" placeholder="如：HP Indigo 12000 · 铜版纸"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>纸张</label><input class="input" id="np-paper" placeholder="铜版纸 157g"></div>
      <div class="field"><label>墨水</label><input class="input" id="np-ink" placeholder="标准四色 / 专色扩展"></div>
    </div>
    <div class="field" style="margin-top:10px"><label>备注</label><input class="input" id="np-notes" placeholder="驱动设置、印刷条件等"></div>
    <div class="dropzone" id="np-drop" style="margin-top:14px">
      ${icon("upload")}
      <div style="font-weight:600">点击或拖入 ICC 文件</div>
      <div style="font-size:12px;margin-top:4px" id="np-file-label">.icc / .icm</div>
    </div>
    <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:16px">
      <button class="btn btn-ghost" id="np-cancel">取消</button>
      <button class="btn" id="np-ok" disabled>${icon("check")}创建工艺</button>
    </div>
  `);

  let iccFile = null;
  const drop = m.modal.querySelector("#np-drop");
  const label = m.modal.querySelector("#np-file-label");
  const okBtn = m.modal.querySelector("#np-ok");

  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".icc,.icm,application/vnd.iccprofile";
  input.style.display = "none";
  drop.appendChild(input);

  const setFile = (f) => {
    iccFile = f;
    label.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(1)} KB` : ".icc / .icm";
    okBtn.disabled = !f || !m.modal.querySelector("#np-name").value.trim();
  };
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => setFile(input.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });
  m.modal.querySelector("#np-name").addEventListener("input", () => setFile(iccFile));

  m.modal.querySelector("#np-cancel").addEventListener("click", m.close);
  okBtn.addEventListener("click", async () => {
    okBtn.disabled = true;
    try {
      const buf = await fileToArrayBuffer(iccFile);
      await apiUploadICC(new Uint8Array(buf), {
        name: m.modal.querySelector("#np-name").value.trim(),
        paper: m.modal.querySelector("#np-paper").value.trim(),
        ink: m.modal.querySelector("#np-ink").value.trim(),
        notes: m.modal.querySelector("#np-notes").value.trim(),
        filename: iccFile.name,
      });
      m.close();
      toast("工艺已创建");
      updateNavCount();
      await loadList(view);
    } catch (e) {
      okBtn.disabled = false;
      toast(e.message, "err");
    }
  });
}

/* ---------- 工艺详情 ---------- */
export async function renderProcessDetail(view, params) {
  view.innerHTML = `<div class="spinner"></div>`;
  let p;
  try {
    p = await apiGet(`/api/processes/${params.id}`);
  } catch (e) {
    view.innerHTML = `<div class="card"><div class="issue issue-error">${icon("xmark")}<span>${esc(e.message)}</span></div></div>`;
    return;
  }

  view.innerHTML = `
    <div class="page-head">
      <div style="display:flex;align-items:center;gap:10px">
        <button class="icon-btn" id="btn-back" style="background:var(--fill);color:var(--label-2)">${icon("chevronR")}</button>
        <div>
          <h1 class="large-title" style="font-size:26px">${esc(p.name)}</h1>
          <p class="page-sub" style="margin-bottom:0">${esc(p.icc_meta.description || "")}</p>
        </div>
      </div>
      <button class="btn btn-danger btn-sm" id="btn-delete">${icon("trash")}删除工艺</button>
    </div>

    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:14px; align-items:start">
      <div class="card">
        <div class="card-title">${icon("stack")}ICC 特性文件</div>
        <dl class="kv" style="margin-top:10px">
          <dt>设备色彩空间</dt><dd>${p.icc_meta.device_space}（${p.icc_meta.channels} 通道）</dd>
          <dt>文件名</dt><dd>${esc(p.icc_original_name)}</dd>
          <dt>大小</dt><dd>${(p.icc_size / 1024).toFixed(1)} KB</dd>
          <dt>描述</dt><dd>${esc(p.icc_meta.description || "-")}</dd>
          <dt>版权</dt><dd>${esc(p.icc_meta.copyright || "-")}</dd>
          ${p.paper ? `<dt>纸张</dt><dd>${esc(p.paper)}</dd>` : ""}
          ${p.ink ? `<dt>墨水</dt><dd>${esc(p.ink)}</dd>` : ""}
          ${p.notes ? `<dt>备注</dt><dd>${esc(p.notes)}</dd>` : ""}
          <dt>创建时间</dt><dd>${p.created_at}</dd>
        </dl>
        <div style="margin-top:14px; display:flex; gap:10px">
          <a class="btn btn-secondary btn-sm" href="/api/processes/${p.id}/icc" download="${esc(p.icc_original_name)}">${icon("download")}下载 ICC</a>
          <button class="btn btn-secondary btn-sm" id="btn-use">${icon("wand")}用它预测配方</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">${icon("target")}工艺色域 · CIE 1931</div>
        <p class="card-desc">经 ICC 采样的设备色域点集（灰点）与 sRGB 参考色域（蓝线），马蹄形为光谱轨迹</p>
        <canvas class="cie-canvas" id="cie"></canvas>
        <div class="cie-legend">
          <span><i class="dot" style="background:linear-gradient(90deg,#4400ff,#00ff00,#ff0000)"></i>光谱轨迹</span>
          <span><i class="dot" style="background:var(--accent)"></i>sRGB</span>
          <span><i class="dot" style="background:#8e8e93"></i>本工艺采样点</span>
        </div>
      </div>
    </div>

    <div class="section-title">该工艺的配方历史</div>
    <div class="card" id="recipe-history"><div class="spinner"></div></div>
  `;

  view.querySelector("#btn-back").addEventListener("click", () => (location.hash = "#/processes"));
  view.querySelector("#btn-use").addEventListener("click", () => (location.hash = "#/recipes"));
  view.querySelector("#btn-delete").addEventListener("click", async () => {
    if (!confirm(`删除工艺「${p.name}」？其 ICC 文件与配方历史关联将一并移除。`)) return;
    try {
      await apiDelete(`/api/processes/${p.id}`);
      toast("工艺已删除");
      updateNavCount();
      location.hash = "#/processes";
    } catch (e) {
      toast(e.message, "err");
    }
  });

  // 色域图（含光谱轨迹马蹄形）
  try {
    const [g, spectrum] = await Promise.all([
      apiGet(`/api/processes/${p.id}/gamut?levels=7`),
      fetchSpectrum().catch(() => null),
    ]);
    drawCie(
      view.querySelector("#cie"),
      [
        { points: SRGB_GAMUT, color: "#007aff", fill: true },
        { points: g.xy_points, color: "#8e8e93" },
      ],
      { spectrum },
    );
  } catch (e) {
    view.querySelector("#cie").replaceWith(Object.assign(document.createElement("div"), {
      textContent: `色域采样失败：${e.message}`,
      style: "color:var(--label-2);padding:20px",
    }));
  }

  // 配方历史
  try {
    const recipes = await apiGet(`/api/recipes?process_id=${p.id}`);
    const box = view.querySelector("#recipe-history");
    if (!recipes.length) {
      box.innerHTML = emptyState({ iconName: "flask", title: "暂无配方记录", desc: "回到配方预测页，用该工艺反查 LAB 配方后会出现在这里。" });
    } else {
      box.innerHTML = recipes.slice(0, 12).map((r) => `
        <div class="group-row">
          <div class="grow">
            <div class="row-title">${r.label ? esc(r.label) : "未命名颜色"} <span class="chip chip-mono">ΔE ${r.delta_e}${r.converged ? " ✓" : ""}</span></div>
            <div class="row-sub">${Object.entries(r.recipe).map(([k, v]) => `${k} ${v}`).join(" · ")} · 目标 Lab(${r.target_lab.map((x) => Math.round(x)).join(",")})</div>
          </div>
          ${sparkline(r.iterations, { width: 90, height: 24, tolerance: r.tolerance })}
        </div>
      `).join("");
    }
  } catch (e) {
    view.querySelector("#recipe-history").textContent = e.message;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
