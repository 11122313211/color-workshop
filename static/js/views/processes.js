/* 工艺库视图（V0.3 重构）：
 * 列表 —— 搜索/筛选/排序工具栏 + 指标卡片（体积/覆盖率/达标率，summary=1 聚合零逐卡请求）
 * 详情 —— 概览指标 + 2D/3D 色域主区（gamut-view 组件）+ ICC 元数据（指纹）+ 配方历史
 * 编辑 —— 新建/编辑合一模态、ICC 替换（sha 变更提示）、批量上传队列
 */

import {
  apiGet, apiPatch, apiDelete, apiUploadICC, apiReplaceICC, fileToArrayBuffer,
} from "../api.js";
import { icon } from "../icons.js";
import {
  toast, emptyState, errorState, skeletonCards, esc,
  labToHex, sparkline, openModal, confirmDanger,
} from "../components.js";
import { createGamutView, SRGB_XY } from "../gamut-view.js";
import { updateNavCount } from "./shell.js";

/* ---------- 列表页 ---------- */

const listState = { search: "", filter: "all", sort: "created", cache: [] };

export async function renderProcessesView(view) {
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">工艺库</h1>
        <p class="page-sub">工艺 = 打印机 + 纸张 + 墨水的组合，每个工艺绑定自己的 ICC 特性文件</p>
      </div>
      <button class="btn" id="btn-new">${icon("plus")}新建工艺</button>
    </div>
    <div class="process-toolbar">
      <div class="input-search-wrap">
        ${icon("search")}
        <input class="input" id="proc-search" placeholder="搜索名称 / 纸张 / 墨水…" aria-label="搜索工艺">
      </div>
      <div class="seg" data-role="filter" aria-label="按色彩空间筛选">
        <button data-f="all" class="active">全部</button>
        <button data-f="CMYK">CMYK</button>
        <button data-f="RGB">RGB</button>
        <button data-f="GRAY">GRAY</button>
      </div>
      <div class="seg" data-role="sort" aria-label="排序">
        <button data-s="created" class="active">最新</button>
        <button data-s="volume">体积</button>
        <button data-s="pass">达标</button>
      </div>
    </div>
    <div id="process-list">${skeletonCards(4)}</div>
  `;

  view.querySelector("#btn-new").addEventListener("click", () =>
    openProcessEditor(view, { mode: "create", onDone: loadList })
  );

  // 搜索（300ms debounce，纯前端过滤）
  let debounce = 0;
  view.querySelector("#proc-search").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      listState.search = e.target.value.trim().toLowerCase();
      renderCards(view);
    }, 300);
  });
  view.querySelector('[data-role="filter"]').addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]");
    if (!b) return;
    listState.filter = b.dataset.f;
    view.querySelectorAll('[data-role="filter"] button').forEach((x) => x.classList.toggle("active", x === b));
    renderCards(view);
  });
  view.querySelector('[data-role="sort"]').addEventListener("click", (e) => {
    const b = e.target.closest("button[data-s]");
    if (!b) return;
    listState.sort = b.dataset.s;
    view.querySelectorAll('[data-role="sort"] button').forEach((x) => x.classList.toggle("active", x === b));
    renderCards(view);
  });

  await loadList(view);
}

async function loadList(view) {
  const box = view.querySelector("#process-list");
  if (!box) return;
  box.innerHTML = `<div class="spinner"></div>`;
  try {
    // summary=1：一次请求带出色域摘要与达标率（消除逐卡 N+1）
    listState.cache = await apiGet("/api/processes?summary=1");
    updateNavCount();
    renderCards(view);
  } catch (e) {
    box.innerHTML = errorState(e.message);
    box.querySelector(".btn-retry").addEventListener("click", () => loadList(view));
  }
}

function visibleProcesses() {
  let docs = listState.cache;
  const q = listState.search;
  if (q) {
    docs = docs.filter((p) =>
      [p.name, p.paper, p.ink, p.notes, p.icc_meta.description]
        .some((s) => s && String(s).toLowerCase().includes(q))
    );
  }
  if (listState.filter !== "all") {
    docs = docs.filter((p) => p.icc_meta.device_space === listState.filter);
  }
  if (listState.sort === "volume") {
    docs = [...docs].sort((a, b) => (b.gamut_summary?.volume ?? -1) - (a.gamut_summary?.volume ?? -1));
  } else if (listState.sort === "pass") {
    docs = [...docs].sort((a, b) => (b.pass_rate ?? -1) - (a.pass_rate ?? -1));
  } else {
    docs = [...docs].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  }
  return docs;
}

function renderCards(view) {
  const box = view.querySelector("#process-list");
  if (!box) return;
  const docs = visibleProcesses();
  if (!listState.cache.length) {
    box.innerHTML = emptyState({
      iconName: "stack", title: "还没有工艺",
      desc: "上传一个 ICC 特性文件即可创建工艺。可以用打印机厂商提供的、RIP 导出的或系统自带的 ICC 文件。",
      action: `<button class="btn" id="empty-new">${icon("plus")}新建工艺</button>`,
    });
    box.querySelector("#empty-new").addEventListener("click", () =>
      openProcessEditor(view, { mode: "create", onDone: loadList })
    );
    return;
  }
  if (!docs.length) {
    box.innerHTML = emptyState({
      iconName: "search", title: "无匹配工艺",
      desc: "换个关键词，或清除搜索与筛选条件。",
      action: `<button class="btn btn-secondary btn-sm" id="clear-search">清除条件</button>`,
    });
    box.querySelector("#clear-search").addEventListener("click", () => {
      listState.search = "";
      listState.filter = "all";
      const search = view.querySelector("#proc-search");
      if (search) search.value = "";
      view.querySelectorAll('[data-role="filter"] button').forEach((x) =>
        x.classList.toggle("active", x.dataset.f === "all")
      );
      renderCards(view);
    });
    return;
  }

  box.innerHTML = `<div class="card-grid">${docs.map(cardHtml).join("")}</div>`;
  box.querySelectorAll(".process-card").forEach((el) => {
    el.addEventListener("click", () => (location.hash = `#/processes/${el.dataset.id}`));
    // 迷你色域凸包缩略图（summary 数据本地绘制，零额外请求）
    drawMiniHull(el.querySelector(".process-mini-cie"), docs.find((d) => d.id === el.dataset.id));
  });
}

function cardHtml(p) {
  const space = p.icc_meta.device_space;
  const s = p.gamut_summary;
  return `
  <div class="card process-card" data-id="${p.id}" tabindex="0" role="button" aria-label="打开工艺 ${esc(p.name)}">
    <div class="process-head">
      <div class="process-mini-cie"></div>
      <div class="grow-truncate">
        <div class="proc-name truncate">${esc(p.name)}</div>
        <div class="proc-desc truncate">${esc(p.icc_meta.description || p.icc_original_name)}</div>
      </div>
    </div>
    <div class="proc-chips">
      <span class="badge ${space === "CMYK" ? "badge-blue" : space === "GRAY" ? "badge-gray" : "badge-gray"}">${space} · ${p.icc_meta.channels}通道</span>
      ${p.paper ? `<span class="chip">${esc(p.paper)}</span>` : ""}
    </div>
    <div class="process-stats">
      <div><b>${s ? formatVolume(s.volume) : "—"}</b><span>色域体积</span></div>
      <div><b>${s ? Math.round(s.coverage_srgb * 100) + "%" : "—"}</b><span>sRGB 覆盖</span></div>
      <div><b>${passRateHtml(p.pass_rate)}</b><span>配方达标</span></div>
    </div>
  </div>`;
}

function formatVolume(v) {
  if (v == null) return "—";
  return v >= 10000 ? `${(v / 10000).toFixed(1)}万` : `${Math.round(v)}`;
}

function passRateHtml(rate) {
  if (rate == null) return `<span class="pass-none">—</span>`;
  const cls = rate >= 0.9 ? "badge-green" : rate >= 0.7 ? "badge-orange" : "badge-red";
  return `<span class="mini-badge ${cls}">${Math.round(rate * 100)}%</span>`;
}

/** 56px 迷你凸包缩略图（仅描边填充，不画点云/网格） */
function drawMiniHull(el, p) {
  const hull = p && p.gamut_summary && p.gamut_summary.hull_xy;
  const canvas = document.createElement("canvas");
  canvas.width = 56 * 2;
  canvas.height = 56 * 2;
  canvas.style.width = "56px";
  canvas.style.height = "56px";
  if (!hull || hull.length < 3) return; // 采样失败保持占位底色
  el.innerHTML = "";
  el.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  ctx.scale(2, 2);
  // 等比映射到 56px（xy 值域 ≤0.8，留 6px 边距）
  const scale = 44 / 0.8;
  const px = (x) => 6 + x * scale;
  const py = (y) => 50 - y * scale;
  ctx.beginPath();
  hull.forEach(([x, y], i) => (i ? ctx.lineTo(px(x), py(y)) : ctx.moveTo(px(x), py(y))));
  ctx.closePath();
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--indigo").trim() || "#5856d6";
  ctx.globalAlpha = 0.14;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

/* ---------- 新建 / 编辑 / 批量上传（合一模态） ---------- */

function openProcessEditor(view, { mode = "create", process = null, onDone } = {}) {
  const isEdit = mode === "edit" && process;
  const m = openModal(`
    <h3 class="card-title">${icon(isEdit ? "pencil" : "upload")}${isEdit ? "编辑工艺" : "新建工艺"}</h3>
    ${
      isEdit
        ? ""
        : `<div class="seg" data-role="upmode" style="margin-bottom:14px">
             <button data-up="single" class="active">单个创建</button>
             <button data-up="batch">批量上传</button>
           </div>`
    }
    <div data-role="fields">
      <div class="field-row">
        <div class="field"><label>工艺名称 *</label><input class="input" id="pe-name" maxlength="80" placeholder="如：HP Indigo 12000 · 铜版纸" value="${isEdit ? esc(process.name) : ""}"></div>
      </div>
      <div class="field-row" style="margin-top:10px">
        <div class="field"><label>纸张</label><input class="input" id="pe-paper" maxlength="80" placeholder="铜版纸 157g" value="${isEdit ? esc(process.paper || "") : ""}"></div>
        <div class="field"><label>墨水</label><input class="input" id="pe-ink" maxlength="80" placeholder="标准四色 / 专色扩展" value="${isEdit ? esc(process.ink || "") : ""}"></div>
      </div>
      <div class="field" style="margin-top:10px"><label>备注</label><input class="input" id="pe-notes" maxlength="300" placeholder="驱动设置、印刷条件等" value="${isEdit ? esc(process.notes || "") : ""}"></div>
    </div>
    <div data-role="icc-area" style="margin-top:14px">
      ${
        isEdit
          ? `<div class="icc-current">
              <div class="icc-line">当前 ICC：${esc(process.icc_original_name)} · ${(process.icc_size / 1024).toFixed(1)} KB
                <span class="chip chip-mono">${process.icc_sha256.slice(0, 8)}</span></div>
              <div class="dropzone dropzone-sm" id="pe-drop">
                ${icon("swap")}
                <div style="font-weight:600">拖入新 ICC 文件可替换</div>
                <div style="font-size:12px;margin-top:4px" id="pe-file-label">不更换则保持现状</div>
              </div>
              <div class="icc-diff" id="pe-diff" hidden></div>
            </div>`
          : `<div class="dropzone" id="pe-drop">
              ${icon("upload")}
              <div style="font-weight:600">点击或拖入 ICC 文件</div>
              <div style="font-size:12px;margin-top:4px" id="pe-file-label">.icc / .icm</div>
            </div>`
      }
    </div>
    <div data-role="queue" class="upload-queue" hidden></div>
    <div style="display:flex; justify-content:flex-end; gap:10px; margin-top:16px">
      <button class="btn btn-ghost" id="pe-cancel">取消</button>
      <button class="btn" id="pe-ok">${icon("check")}${isEdit ? "保存修改" : "创建工艺"}</button>
    </div>
  `);

  const modal = m.modal;
  const okBtn = modal.querySelector("#pe-ok");
  const nameInput = modal.querySelector("#pe-name");
  let upMode = "single";

  const readForm = () => ({
    name: nameInput.value.trim(),
    paper: modal.querySelector("#pe-paper").value.trim(),
    ink: modal.querySelector("#pe-ink").value.trim(),
    notes: modal.querySelector("#pe-notes").value.trim(),
  });

  /* —— 文件选择（单个：一个 iccFile；批量：文件队列）—— */
  let iccFile = null;
  const drop = modal.querySelector("#pe-drop");
  const label = modal.querySelector("#pe-file-label");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".icc,.icm,application/vnd.iccprofile";
  if (!isEdit) input.multiple = true;
  input.style.display = "none";
  drop.appendChild(input);

  const setFile = (f) => {
    iccFile = f;
    label.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(1)} KB` : isEdit ? "不更换则保持现状" : ".icc / .icm";
    if (iccFile && !isEdit) {
      // 默认工艺名 = 文件名去扩展名（仅当用户没填名字时）
      if (!nameInput.value.trim()) nameInput.value = f.name.replace(/\.(ic[cm]|iccprofile)$/i, "");
    }
    refreshOk();
  };
  input.addEventListener("change", () => {
    const files = [...input.files];
    if (!isEdit && files.length > 1) {
      openQueue(files);
    } else {
      setFile(files[0] || null);
      const diff = modal.querySelector("#pe-diff");
      if (isEdit && diff) diff.hidden = !iccFile;
    }
  });

  function refreshOk() {
    if (isEdit) {
      okBtn.disabled = !readForm().name;
    } else {
      okBtn.disabled = upMode === "batch" ? true : !(iccFile && readForm().name);
    }
  }
  nameInput.addEventListener("input", refreshOk);

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    const files = [...e.dataTransfer.files];
    if (!files.length) return;
    if (!isEdit && files.length > 1) openQueue(files);
    else setFile(files[0]);
  });

  /* —— 批量模式（仅 create）—— */
  const queueBox = modal.querySelector("[data-role='queue']");
  let queue = [];
  function openQueue(files) {
    upMode = "batch";
    modal.querySelector('[data-role="upmode"]').querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.dataset.up === "batch")
    );
    queue = files.map((f, i) => ({ file: f, name: f.name.replace(/\.(ic[cm]|iccprofile)$/i, ""), status: "pending", pct: 0, doc: null }));
    iccFile = null;
    label.textContent = `${files.length} 个文件待上传`;
    renderQueue();
    refreshOk();
    okBtn.textContent = "开始上传";
  }

  function renderQueue() {
    queueBox.hidden = !queue.length;
    queueBox.innerHTML = queue
      .map(
        (q, i) => `
      <div class="uq-row" data-i="${i}">
        <input class="input uq-name" value="${esc(q.name)}" aria-label="工艺名" ${q.status === "uploading" ? "disabled" : ""}>
        <span class="uq-meta">${(q.file.size / 1024).toFixed(0)} KB</span>
        <span class="uq-status ${
          q.status === "ok" ? "ok" : q.status === "failed" ? "failed" : ""
        }">${q.status === "pending" ? "待上传" : q.status === "uploading" ? `${Math.round(q.pct * 100)}%` : q.status === "ok" ? "✓ 完成" : "✗ 失败"}</span>
        ${q.status === "failed" ? `<button class="btn btn-secondary btn-sm" data-retry="${i}">重试</button>` : ""}
      </div>`
      )
      .join("");
    queueBox.querySelectorAll(".uq-name").forEach((inp, i) =>
      inp.addEventListener("input", () => (queue[i].name = inp.value.trim()))
    );
    queueBox.querySelectorAll("[data-retry]").forEach((b) =>
      b.addEventListener("click", () => runQueue(+b.dataset.retry))
    );
  }

  async function runQueue(startIdx = 0) {
    okBtn.disabled = true;
    for (let i = startIdx; i < queue.length; i++) {
      const q = queue[i];
      if (q.status === "ok") continue;
      q.status = "uploading";
      q.pct = 0;
      renderQueue();
      try {
        const buf = await fileToArrayBuffer(q.file);
        q.doc = await apiUploadICC(new Uint8Array(buf), { name: q.name || q.file.name, filename: q.file.name }, {
          onProgress: (pct) => {
            q.pct = pct;
            const row = queueBox.querySelector(`.uq-row[data-i="${i}"] .uq-status`);
            if (row) row.textContent = `${Math.round(pct * 100)}%`;
          },
        });
        q.status = "ok";
      } catch (e) {
        q.status = "failed";
        q.err = e.message;
      }
      renderQueue();
    }
    const okCount = queue.filter((q) => q.status === "ok").length;
    updateNavCount();
    if (okCount) {
      toast(`已创建 ${okCount} 个工艺`, "ok");
      m.close();
      onDone && onDone();
    } else {
      toast("全部上传失败，请检查文件后重试", "err");
      okBtn.disabled = false;
    }
  }

  /* —— 模式切换（仅 create）—— */
  const upmodeSeg = modal.querySelector('[data-role="upmode"]');
  if (upmodeSeg) {
    upmodeSeg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-up]");
      if (!b) return;
      upMode = b.dataset.up;
      upmodeSeg.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
      queue = [];
      renderQueue();
      refreshOk();
      okBtn.innerHTML = `${icon("check")}${upMode === "batch" ? "开始上传" : "创建工艺"}`;
      if (upMode === "batch") toast("在下方拖入多个 ICC 文件（也可多选）", "warn");
    });
  }

  modal.querySelector("#pe-cancel").addEventListener("click", m.close);
  okBtn.addEventListener("click", async () => {
    const form = readForm();
    if (!form.name) return toast("请填写工艺名称", "warn");
    if (isEdit) {
      okBtn.disabled = true;
      try {
        await apiPatch(`/api/processes/${process.id}`, form);
        if (iccFile) {
          const buf = await fileToArrayBuffer(iccFile);
          const resp = await apiReplaceICC(process.id, new Uint8Array(buf), iccFile.name);
          if (resp.space_changed) {
            toast(`ICC 已替换且设备空间变为 ${resp.process.icc_meta.device_space}，请重新预测验证`, "warn", { });
          } else {
            toast("ICC 已替换，色域数据将按新指纹重算", "ok");
          }
        } else {
          toast("工艺已保存");
        }
        m.close();
        onDone && onDone();
      } catch (e) {
        okBtn.disabled = false;
        toast(e.message, "err");
      }
      return;
    }
    if (upMode === "batch") {
      if (!queue.length) return toast("请先选择 ICC 文件", "warn");
      return runQueue();
    }
    if (!iccFile) return toast("请选择 ICC 文件", "warn");
    okBtn.disabled = true;
    try {
      const buf = await fileToArrayBuffer(iccFile);
      await apiUploadICC(new Uint8Array(buf), { ...form, filename: iccFile.name }, {
        onProgress: (pct) => (okBtn.textContent = pct < 1 ? `上传 ${Math.round(pct * 100)}%` : "创建中…"),
      });
      m.close();
      toast("工艺已创建");
      updateNavCount();
      onDone && onDone();
    } catch (e) {
      okBtn.disabled = false;
      okBtn.innerHTML = `${icon("check")}创建工艺`;
      toast(e.message, "err");
    }
  });
  refreshOk();
}

/* ---------- 工艺详情 ---------- */

export async function renderProcessDetail(view, params) {
  view.innerHTML = `<div class="spinner"></div>`;
  let p;
  try {
    p = await apiGet(`/api/processes/${params.id}`);
  } catch (e) {
    view.innerHTML = errorState(e.message, { retry: false });
    return;
  }

  const gamutPromise = apiGet(`/api/processes/${p.id}/gamut?levels=7`);
  const recipesPromise = apiGet(`/api/recipes?process_id=${p.id}`);

  view.innerHTML = `
    <div class="page-head">
      <div style="display:flex;align-items:center;gap:10px;min-width:0">
        <button class="icon-btn btn-back" aria-label="返回工艺库" style="background:var(--fill);color:var(--label-2)">${icon("chevronL")}</button>
        <div style="min-width:0">
          <h1 class="large-title" style="font-size:26px">${esc(p.name)}</h1>
          <p class="page-sub" style="margin-bottom:0">${esc(p.icc_meta.description || "")}</p>
        </div>
      </div>
      <div class="head-actions">
        <button class="btn btn-sm" id="btn-use">${icon("wand")}用此工艺预测</button>
        <button class="btn btn-secondary btn-sm" id="btn-edit">${icon("pencil")}编辑</button>
        <button class="icon-btn" id="btn-delete" aria-label="删除工艺" style="color:var(--red)">${icon("trash")}</button>
      </div>
    </div>

    <div class="stat-grid">
      <div class="card stat"><div class="stat-val" id="st-volume"><span class="spinner"></span></div><div class="stat-label">色域体积（Lab 包围盒）</div></div>
      <div class="card stat"><div class="stat-val" id="st-lrange"><span class="spinner"></span></div><div class="stat-label">L* 明度范围</div></div>
      <div class="card stat"><div class="stat-val" id="st-cov"><span class="spinner"></span></div><div class="stat-label">sRGB 覆盖率</div></div>
      <div class="card stat"><div class="stat-val" id="st-pass"><span class="spinner"></span></div><div class="stat-label">配方达标率（近 20 条）</div></div>
    </div>

    <div class="card gamut-card">
      <div class="gamut-card-head">
        <div class="card-title">${icon("target")}工艺色域</div>
        <div class="cie-legend">
          <span><i class="dot" style="background:linear-gradient(90deg,#4400ff,#00ff00,#ff0000)"></i>光谱轨迹</span>
          <span><i class="dot legend-srgb"></i>sRGB</span>
          <span><i class="dot legend-proc"></i>工艺凸包 · 采样点按 L* 着色</span>
        </div>
      </div>
      <div id="gamut-view"></div>
    </div>

    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:14px; align-items:start">
      <div class="card">
        <div class="card-title">${icon("stack")}ICC 特性文件</div>
        <dl class="kv" style="margin-top:10px">
          <dt>设备色彩空间</dt><dd>${p.icc_meta.device_space}（${p.icc_meta.channels} 通道）</dd>
          <dt>ICC 指纹</dt><dd><span class="chip chip-mono">${p.icc_sha256.slice(0, 8)}</span></dd>
          <dt>文件名</dt><dd>${esc(p.icc_original_name)}</dd>
          <dt>大小</dt><dd>${(p.icc_size / 1024).toFixed(1)} KB</dd>
          <dt>描述</dt><dd>${esc(p.icc_meta.description || "-")}</dd>
          <dt>版权</dt><dd>${esc(p.icc_meta.copyright || "-")}</dd>
          ${p.paper ? `<dt>纸张</dt><dd>${esc(p.paper)}</dd>` : ""}
          ${p.ink ? `<dt>墨水</dt><dd>${esc(p.ink)}</dd>` : ""}
          ${p.notes ? `<dt>备注</dt><dd>${esc(p.notes)}</dd>` : ""}
          <dt>创建时间</dt><dd>${p.created_at}</dd>
        </dl>
        <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap">
          <a class="btn btn-secondary btn-sm" href="/api/processes/${p.id}/icc" download="${esc(p.icc_original_name)}">${icon("download")}下载 ICC</a>
          <button class="btn btn-secondary btn-sm" id="btn-replace">${icon("swap")}替换 ICC</button>
        </div>
      </div>

      <div>
        <div class="section-title" style="margin-top:0">该工艺的配方历史</div>
        <div class="card" id="recipe-history"><div class="spinner"></div></div>
      </div>
    </div>
  `;

  const reload = () => renderProcessDetail(view, params);

  view.querySelector(".btn-back").addEventListener("click", () => (location.hash = "#/processes"));
  view.querySelector("#btn-use").addEventListener("click", () => (location.hash = `#/recipes?process=${p.id}`));
  view.querySelector("#btn-edit").addEventListener("click", () =>
    openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
  );
  view.querySelector("#btn-replace").addEventListener("click", () =>
    openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
  );
  view.querySelector("#btn-delete").addEventListener("click", async () => {
    const ok = await confirmDanger({
      title: `删除工艺「${p.name}」？`,
      message: "其 ICC 文件与全部配方历史将一并移除，此操作不可撤销。",
      confirmText: "仍要删除",
    });
    if (!ok) return;
    try {
      await apiDelete(`/api/processes/${p.id}`);
      toast("工艺已删除");
      updateNavCount();
      location.hash = "#/processes";
    } catch (e) {
      toast(e.message, "err");
    }
  });

  // 色域主区（2D/3D 组件）+ 概览指标
  try {
    const [gamut, recipes] = await Promise.all([gamutPromise, recipesPromise]);
    if (!view.isConnected) return; // 用户已切走
    view.querySelector("#st-volume").textContent = formatVolume(gamut.volume);
    view.querySelector("#st-lrange").textContent = `${gamut.L_range[0]} – ${gamut.L_range[1]}`;
    view.querySelector("#st-cov").textContent = `${Math.round(gamut.coverage_srgb * 100)}%`;
    const recent = recipes.slice(0, 20);
    view.querySelector("#st-pass").innerHTML = recent.length
      ? passRateHtml(recent.filter((r) => r.converged).length / recent.length)
      : `<span class="pass-none">—</span>`;

    createGamutView(view.querySelector("#gamut-view"), {
      getData: () => gamutPromise,
      levels: 7,
    });

    renderHistory(view, recipes);
  } catch (e) {
    const holder = view.querySelector("#gamut-view");
    if (holder) holder.innerHTML = errorState(`色域数据加载失败：${e.message}`);
  }
}

function renderHistory(view, recipes) {
  const box = view.querySelector("#recipe-history");
  if (!recipes.length) {
    box.innerHTML = emptyState({
      iconName: "flask", title: "暂无配方记录",
      desc: "回到配方预测页，用该工艺反查 LAB 配方后会出现在这里。",
    });
    return;
  }
  box.innerHTML = recipes.slice(0, 12)
    .map(
      (r) => `
      <a class="group-row hist-row" href="#/recipes">
        <div class="grow">
          <div class="row-title">${r.label ? esc(r.label) : "未命名颜色"} ${statusChip(r)}</div>
          <div class="row-sub">${Object.entries(r.recipe).map(([k, v]) => `${k} ${v}`).join(" · ")} · 目标 Lab(${r.target_lab.map((x) => Math.round(x)).join(",")})</div>
        </div>
        ${sparkline(r.iterations, { width: 90, height: 24, tolerance: r.tolerance })}
      </a>`
    )
    .join("");
}

function statusChip(r) {
  const cls = r.converged ? "badge-green" : r.delta_e <= 5 ? "badge-orange" : "badge-red";
  return `<span class="mini-badge ${cls}">ΔE ${r.delta_e}${r.converged ? " ✓" : ""}</span>`;
}
