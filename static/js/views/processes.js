/* 工艺库视图（V0.3 重构）：
 * 列表 —— 搜索/筛选/排序工具栏 + 指标卡片（体积/覆盖率/达标率，summary=1 聚合零逐卡请求）
 * 详情 —— 概览指标 + 2D/3D 色域主区（gamut-view 组件）+ ICC 元数据（指纹）+ 配方历史
 * 编辑 —— 新建/编辑合一模态、ICC 替换（sha 变更提示）、批量上传队列
 */

import {
  apiGet, apiPatch, apiDelete, apiUploadICC, apiReplaceICC, fileToArrayBuffer,
  apiPostRaw, downloadDataUri,
} from "../api.js";
import { icon } from "../icons.js";
import {
  toast, emptyState, errorState, skeletonCards, esc,
  labToHex, sparkline, openModal, confirmDanger, drawCie, fetchSpectrum,
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

/* 补偿模型卡（V0.4）：实测闭环沉淀的工艺级墨量偏置 */
function compensationCard(p) {
  const c = p.compensation;
  const names = { CMYK: ["C", "M", "Y", "K"], RGB: ["R", "G", "B"], GRAY: ["K"] }[
    p.icc_meta.device_space
  ] || [];
  const chips = (c.bias_device || [])
    .map((v, i) => {
      if (Math.abs(v) < 0.0005) return "";
      const pct = v * 100;
      return `<span class="chip chip-mono">${names[i] ?? i} ${pct > 0 ? "+" : ""}${pct.toFixed(1)}%</span>`;
    })
    .join("");
  return `
    <div class="card" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:12px 16px">
      <span class="card-title" style="margin:0; display:inline-flex; align-items:center; gap:6px">
        ${icon("refresh")}工艺补偿模型 <span class="chip">n=${c.n}</span>
      </span>
      <span class="page-sub" style="margin:0; flex:1; min-width:180px">实测闭环沉淀的墨量偏置，预测时自动应用</span>
      ${chips || `<span class="chip chip-mono">偏置≈0</span>`}
      <button class="btn btn-secondary btn-sm" id="btn-comp-reset">重置</button>
    </div>`;
}

/* 验证样张模态（V0.5 M3）：双点重复布局的 A4 CMYK 样张，PDF/TIFF 下载 */
async function openTestformModal(view, p) {
  const modal = openModal(`
    <h3 class="title">${icon("doc")}生成验证样张 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <p class="swatch-meta" style="margin-top:4px">A4 CMYK · 设备值直出（不做 ICC 转换，测的是设备自身表现）。
      每个测量目标左右双点重复——打印后逐点测量并导入「监控 → 测量数据」，同标签两点自动构成复测对。</p>
    <div style="display:flex; align-items:center; gap:10px; margin-top:12px">
      <div class="field" style="margin:0">
        <label>分辨率</label>
        <select id="tf-dpi" class="input"><option>300</option><option>600</option></select>
      </div>
      <span style="flex:1"></span>
      <button class="btn btn-secondary" data-tf-close type="button">关闭</button>
      <button class="btn tf-go" type="button">${icon("printer")}生成样张</button>
    </div>
    <div id="tf-result" style="margin-top:14px"></div>
  `);
  const $ = (sel) => modal.modal.querySelector(sel);

  $(".tf-go").addEventListener("click", async () => {
    const dpi = Number($("#tf-dpi").value || 300);
    const goBtn = $(".tf-go");
    goBtn.disabled = true;
    $("#tf-result").innerHTML = `<p class="swatch-meta">生成中…</p>`;
    try {
      const res = await apiPostRaw("/api/qc/testform", { process_id: p.id, dpi }, new Uint8Array(0));
      const pairs = res.pairs || [];
      $("#tf-result").innerHTML = `
        <img src="${res.preview_b64}" alt="样张预览"
          style="width:100%; max-width:340px; display:block; margin:0 auto; border-radius:10px; border:1px solid var(--separator)">
        <div class="swatch-meta" style="margin-top:10px">
          ${res.page_mm[0]}×${res.page_mm[1]}mm @${res.dpi}dpi · ${res.labels.length} 种标签 · ${pairs.length} 个双点复测对
        </div>
        <div style="display:flex; gap:10px; margin-top:12px; flex-wrap:wrap">
          <button class="btn tf-pdf" type="button">${icon("download")}下载 PDF（打印）</button>
          <button class="btn btn-secondary tf-tiff" type="button">${icon("download")}下载 TIFF</button>
        </div>
        <details style="margin-top:10px"><summary class="swatch-meta">双点配对坐标（mm）</summary>
          <div class="kv-list swatch-meta" style="max-height:180px; overflow:auto; margin-top:6px">
            ${pairs.slice(0, 60).map((q) => `<div class="chip chip-mono">${esc(q.label)}: A(${q.pos_a_mm.join(",")}) ↔ B(${q.pos_b_mm.join(",")})</div>`).join(" ")}
          </div>
        </details>`;
      $(".tf-pdf").addEventListener("click", () =>
        downloadDataUri(`data:application/pdf;base64,${res.pdf_b64}`, `testform-${p.name}-${dpi}dpi.pdf`));
      $(".tf-tiff").addEventListener("click", () =>
        downloadDataUri(`data:image/tiff;base64,${res.tiff_b64}`, `testform-${p.name}-${dpi}dpi.tif`));
    } catch (e) {
      $("#tf-result").innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
    }
    goBtn.disabled = false;
  });

  modal.modal.querySelector("[data-tf-close]").addEventListener("click", () => modal.close());
}

/* 打印校准向导（V0.7）：① 生成校准版+参考表 → ② 导入测量+双基准报告 → ③ 应用墨量建议+导出。
 * 双基准：工艺 ICC 预期（模型误差）+ ISO 12647-2 固体参考（绝对达标，仅涂布纸）。
 * 校准建议一键写入补偿模型（预测联动）；校正曲线/特性数据导出供外部专业软件生成 ICC。 */
async function openCalibrationModal(view, p) {
  const state = { step: 1, form: null, sessionId: "", report: null, applied: null };
  const OVERALL_BADGE = {
    ok: `<span class="badge badge-green">${icon("check")}达标</span>`,
    suggest: `<span class="badge badge-orange">建议校准</span>`,
    needed: `<span class="badge badge-red">需校准</span>`,
    "n/a": `<span class="badge" style="background:var(--fill); color:var(--label-2)">无校准数据</span>`,
  };
  const sw = (lab) => `<span class="swatch" style="background:${labToHex(lab)}; display:inline-block; width:22px; height:16px; border-radius:4px; border:1px solid var(--separator)"></span>`;

  const modal = openModal(`
    <h3 class="title">${icon("printer")}打印校准 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <div id="cal-steps" class="swatch-meta" style="display:flex; gap:14px; margin-top:6px"></div>
    <div id="cal-body" style="margin-top:12px"></div>
  `);
  const $ = (sel) => modal.modal.querySelector(sel);

  const renderSteps = () => {
    const names = ["① 生成校准版", "② 测量与报告", "③ 应用与导出"];
    $("#cal-steps").innerHTML = names
      .map((n, i) => `<span style="${state.step === i + 1 ? "color:var(--text); font-weight:700" : "color:var(--label-3)"}">${n}</span>`)
      .join(`<span style="color:var(--label-3)">→</span>`);
  };

  const renderStep1 = () => {
    renderSteps();
    $("#cal-body").innerHTML = `
      <p class="swatch-meta">A4 编号色块矩阵（28 色块 × 左右双点）：单色梯级 100/75/50/25/10、三色叠印、灰平衡、纸白。
        打印后用色差仪<b>按 P 编号逐块测量两遍</b>（两遍分别导出，构成复测对——单遍数据不可信）。</p>
      <div style="display:flex; align-items:center; gap:10px; margin-top:12px">
        <div class="field" style="margin:0">
          <label>分辨率</label>
          <select id="cal-dpi" class="input"><option>300</option><option>600</option></select>
        </div>
        <span style="flex:1"></span>
        <button class="btn cal-gen" type="button">${icon("printer")}生成校准版</button>
      </div>
      <div id="cal-gen-result" style="margin-top:12px"></div>`;
    $(".cal-gen").addEventListener("click", async () => {
      const dpi = Number($("#cal-dpi").value || 300);
      const btn = $(".cal-gen");
      btn.disabled = true;
      $("#cal-gen-result").innerHTML = `<p class="swatch-meta">生成中…</p>`;
      try {
        state.form = await apiPostRaw("/api/qc/calibration/form", { process_id: p.id, dpi }, new Uint8Array(0));
        const ref = state.form.reference;
        $("#cal-gen-result").innerHTML = `
          <img src="${state.form.preview_b64}" alt="校准版预览"
            style="width:100%; max-width:340px; display:block; margin:0 auto; border-radius:10px; border:1px solid var(--separator)">
          <div class="swatch-meta" style="margin-top:10px">
            ${state.form.page_mm[0]}×${state.form.page_mm[1]}mm @${dpi}dpi · 28 色块 × 2 双点 ·
            参考基准：ICC 预期${ref.iso_available ? " + ISO 12647-2（涂布纸）" : "（纸张非涂布/未分类，ISO 列留空）"}
          </div>
          <div style="display:flex; gap:10px; margin-top:12px; flex-wrap:wrap">
            <button class="btn cal-pdf" type="button">${icon("download")}下载 PDF（打印）</button>
            <button class="btn btn-secondary cal-tiff" type="button">${icon("download")}下载 TIFF</button>
            <button class="btn btn-secondary cal-ref" type="button">${icon("download")}下载参考表（CGATS）</button>
          </div>
          <div style="margin-top:14px; text-align:right">
            <button class="btn cal-next1" type="button">下一步：测量与报告 →</button>
          </div>`;
        $(".cal-pdf").addEventListener("click", () =>
          downloadDataUri(`data:application/pdf;base64,${state.form.pdf_b64}`, `calibration-${p.name}-${dpi}dpi.pdf`));
        $(".cal-tiff").addEventListener("click", () =>
          downloadDataUri(`data:image/tiff;base64,${state.form.tiff_b64}`, `calibration-${p.name}-${dpi}dpi.tif`));
        $(".cal-ref").addEventListener("click", () => downloadDataUri(
          `data:text/plain;base64,${btoa(unescape(encodeURIComponent(ref.cgats_reference || "")))}`,
          `calibration-reference-${p.name}.txt`));
        $(".cal-next1").addEventListener("click", renderStep2);
      } catch (e) {
        $("#cal-gen-result").innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
      }
      btn.disabled = false;
    });
  };

  const renderStep2 = () => {
    renderSteps();
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    const localIso = now.toISOString().slice(0, 16);
    $("#cal-body").innerHTML = `
      <p class="swatch-meta">导入色差仪导出的测量文本（含 CIE Lab）。<b>两遍测量分别导出两个文件</b>依次导入，
        同色块两点自动构成复测对；单遍数据会被判为 warning 不进入报告。</p>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px">
        <div class="field"><label>仪器 *</label><input id="cal-instrument" class="input" value="色差仪（校准）"></div>
        <div class="field"><label>校准时间 *</label><input id="cal-calibrated" class="input" type="datetime-local" value="${localIso}"></div>
      </div>
      <div class="field" style="margin-top:10px">
        <label>测量文件（可多选，按测量顺序）</label>
        <input id="cal-files" class="input" type="file" multiple accept=".txt,.csv,.it8,.cgats">
      </div>
      <div style="margin-top:10px"><button class="btn cal-import" type="button">${icon("upload")}导入并生成报告</button></div>
      <div id="cal-report" style="margin-top:12px"></div>
      <div style="margin-top:14px; display:flex; justify-content:space-between">
        <button class="btn btn-secondary cal-back2" type="button">← 上一步</button>
        <button class="btn cal-next2" type="button" disabled>下一步：应用与导出 →</button>
      </div>`;
    $(".cal-back2").addEventListener("click", renderStep1);

    const renderReport = (rep) => {
      const sugChips = (rep.ink_suggestions || []).map((s) => {
        if (!s.available) return `<span class="chip chip-mono" style="color:var(--label-3)">${s.channel} 缺固体实测</span>`;
        if (Math.abs(s.delta_pct) < 0.5) return `<span class="chip chip-mono">${s.channel} ±0%</span>`;
        const warn = s.beyond_range ? ` ⚠超出墨量修正范围` : "";
        return `<span class="chip chip-mono">${s.channel} ${s.delta_pct > 0 ? "+" : ""}${s.delta_pct}%${warn}</span>`;
      }).join(" ");
      const rows = (rep.patches || []).map((row) => `
        <tr>
          <td class="swatch-meta">${row.index} ${esc(row.label)}</td>
          <td>${sw(row.measured_lab)}</td>
          <td>${sw(row.expected_lab)}</td>
          <td class="swatch-meta chip-mono">${row.de_expected}</td>
          <td class="swatch-meta chip-mono">${row.de_iso ?? "-"}</td>
          <td>${row.pass_iso === null ? `<span class="swatch-meta" style="color:var(--label-3)">-</span>`
            : row.pass_iso ? `<span class="badge badge-green">达标</span>` : `<span class="badge badge-red">超差</span>`}</td>
        </tr>`).join("");
      $("#cal-report").innerHTML = `
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
          ${OVERALL_BADGE[rep.overall] || ""}
          <span class="swatch-meta">命中 ${rep.matched}/${rep.patch_count_total} ·
            ΔE(ICC) max ${rep.summary.max_de_expected ?? "-"} / avg ${rep.summary.avg_de_expected ?? "-"}</span>
          ${rep.paper_class === "uncoated" || !rep.iso_available ? `<span class="swatch-meta" style="color:var(--label-3)">ISO 参考列：非涂布纸暂未收录</span>` : ""}
        </div>
        ${sugChips ? `<div style="margin-top:8px">${sugChips}</div>` : ""}
        <div style="max-height:300px; overflow:auto; margin-top:10px">
          <table class="lab-table" style="width:100%">
            <thead><tr><th>色块</th><th>实测</th><th>预期</th><th>ΔE ICC</th><th>ΔE ISO</th><th>ISO 判定</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
      const canProceed = rep.overall !== "n/a";
      $(".cal-next2").disabled = !canProceed;
      if (canProceed) $(".cal-next2").addEventListener("click", renderStep3);
    };

    $(".cal-import").addEventListener("click", async () => {
      const files = $("#cal-files").files;
      if (!files.length) { toast("请先选择测量文件", "err"); return; }
      const btn = $(".cal-import");
      btn.disabled = true;
      $("#cal-report").innerHTML = `<p class="swatch-meta">导入中…</p>`;
      try {
        for (let i = 0; i < files.length; i++) {
          const bytes = new Uint8Array(await fileToArrayBuffer(files[i]));
          const query = { filename: files[i].name, process_id: p.id, repeat_mode: "single" };
          if (state.sessionId) {
            query.session_id = state.sessionId;
            query.repeat_mode = "repeat";  // 追加文件 = 复测组，会话整体重分级
          } else {
            query.instrument = $("#cal-instrument").value.trim() || "色差仪（校准）";
            query.calibrated_at = $("#cal-calibrated").value;
            query.condition = "M1";
          }
          const res = await apiPostRaw("/api/qc/measurements/import", query, bytes);
          state.sessionId = res.session.id;
        }
        state.report = await apiGet(`/api/qc/calibration?session_id=${encodeURIComponent(state.sessionId)}`);
        renderReport(state.report);
      } catch (e) {
        $("#cal-report").innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
      }
      btn.disabled = false;
    });
  };

  const renderStep3 = () => {
    renderSteps();
    const rep = state.report;
    const sugRows = (rep.ink_suggestions || []).filter((s) => s.available).map((s) => `
      <div class="chip chip-mono" style="padding:8px 12px">
        <b>${s.channel}</b>&nbsp; 实测 L ${s.measured_lab[0]} → 建议 ${s.delta_pct > 0 ? "+" : ""}${s.delta_pct}%
        （修正后 ${s.corrected_pct}%）${s.beyond_range ? ` <span style="color:var(--orange)">⚠ 超出墨量可修正范围</span>` : ""}
      </div>`).join("");
    $("#cal-body").innerHTML = `
      <p class="swatch-meta">把逐通道墨量建议写入工艺补偿模型（立即作用于后续配方预测），
        并导出校正输出——正式 ICC 建议将导出的特性数据交给 ArgyllCMS / i1Profiler 等专业软件生成。</p>
      <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:10px">${sugRows || `<span class="swatch-meta" style="color:var(--label-3)">无可下发的建议</span>`}</div>
      <div style="display:flex; gap:10px; margin-top:14px; flex-wrap:wrap">
        <button class="btn cal-apply" type="button">${icon("check")}应用到补偿模型</button>
        <button class="btn btn-secondary cal-csv" type="button">${icon("download")}校正曲线 CSV</button>
        <button class="btn btn-secondary cal-char" type="button">${icon("download")}实测特性数据 CGATS</button>
        <button class="btn btn-secondary cal-corr" type="button">${icon("download")}校正后特性数据 CGATS</button>
      </div>
      <div id="cal-apply-result" style="margin-top:10px"></div>
      <p class="swatch-meta" style="margin-top:12px; color:var(--label-2)">
        建议流程：应用建议 → 重新打印校准版并测量 → 报告应转向「达标」。特性数据为「校准后设备」的一阶近似，
        正式建 ICC 前请用应用校正后的印刷条件重新测量。</p>
      <div style="margin-top:14px">
        <button class="btn btn-secondary cal-back3" type="button">← 上一步</button>
      </div>`;
    $(".cal-back3").addEventListener("click", renderStep2);
    $(".cal-csv").addEventListener("click", () => downloadDataUri(
      `data:text/csv;base64,${btoa(unescape(encodeURIComponent(state.report.exports.curves_csv)))}`,
      `calibration-curves-${p.name}.csv`));
    $(".cal-char").addEventListener("click", () => downloadDataUri(
      `data:text/plain;base64,${btoa(unescape(encodeURIComponent(state.report.exports.characterization_cgats)))}`,
      `characterization-${p.name}.txt`));
    $(".cal-corr").addEventListener("click", () => downloadDataUri(
      `data:text/plain;base64,${btoa(unescape(encodeURIComponent(state.report.exports.corrected_cgats)))}`,
      `characterization-corrected-${p.name}.txt`));
    $(".cal-apply").addEventListener("click", async () => {
      const ok = await confirmDanger({
        title: "应用校准建议？",
        message: "逐通道墨量偏置将写入补偿模型并立即作用于配方预测（可随时在补偿卡重置）。",
        confirmText: "应用",
      });
      if (!ok) return;
      const btn = $(".cal-apply");
      btn.disabled = true;
      try {
        state.applied = await apiPostRaw("/api/qc/calibration/apply", { session_id: state.sessionId }, new Uint8Array(0));
        const chips = (state.applied.bias_device || [])
          .map((v, i) => Math.abs(v) < 0.0005 ? "" :
            `<span class="chip chip-mono">${"CMYK"[i]} ${v * 100 > 0 ? "+" : ""}${(v * 100).toFixed(1)}%</span>`).join(" ");
        $("#cal-apply-result").innerHTML = `
          <div class="badge badge-green">${icon("check")}已写入补偿模型（n=${state.applied.n}）</div>
          <div style="margin-top:8px">${chips || `<span class="swatch-meta" style="color:var(--label-3)">偏置≈0（无需调整）</span>`}</div>`;
        toast("校准建议已应用", "ok");
      } catch (e) {
        $("#cal-apply-result").innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
      }
      btn.disabled = false;
    });
  };

  renderStep1();
}

/* 色域对比模态（V0.4）：双工艺凸包叠加 + 交集多边形 + xy 面积指标 */
async function openCompareModal(view, p) {  let others = [];
  try {
    others = (await apiGet("/api/processes")).filter((x) => x.id !== p.id);
  } catch (_) { /* 列表失败也可手动刷新重试 */ }

  const { modal, close } = openModal(`
    <div class="card-title" style="display:flex; align-items:center; gap:8px">
      ${icon("compare")}色域对比 · <span style="color:var(--label-2)">${esc(p.name)}</span>
    </div>
    <div id="cmp-setup" style="margin-top:12px">
      ${others.length ? `
        <label class="page-sub" style="margin:0">对比工艺</label>
        <select id="cmp-other" style="width:100%; margin-top:8px; padding:9px 12px; border:1px solid var(--separator); border-radius:var(--radius-btn); background:var(--fill); color:var(--label); font-size:14px">
          ${others.map((x) => `<option value="${x.id}">${esc(x.name)}（${x.icc_meta.device_space}）</option>`).join("")}
        </select>
        <div style="display:flex; gap:10px; margin-top:14px; justify-content:flex-end">
          <button class="btn btn-secondary btn-sm" id="cmp-cancel">关闭</button>
          <button class="btn btn-sm" id="cmp-go">${icon("compare")}开始对比</button>
        </div>`
      : `<div class="issue issue-warn">${icon("warn")}<span>工艺库中目前只有这一个工艺，先创建另一个工艺再对比。</span></div>`}
    </div>
    <div id="cmp-result"></div>
  `);

  modal.querySelector("#cmp-cancel")?.addEventListener("click", close);
  const goBtn = modal.querySelector("#cmp-go");
  if (goBtn) {
    goBtn.addEventListener("click", async () => {
      const otherId = modal.querySelector("#cmp-other").value;
      const otherName = others.find((x) => x.id === otherId)?.name || "";
      goBtn.disabled = true;
      goBtn.innerHTML = `<span class="spinner"></span>采样中…`;
      try {
        const data = await apiGet(
          `/api/processes/${p.id}/gamut/compare?other=${otherId}&levels=6`
        );
        modal.querySelector("#cmp-setup").hidden = true;
        renderCompareResult(modal.querySelector("#cmp-result"), p, otherName, data);
      } catch (e) {
        toast(e.message, "err");
        goBtn.disabled = false;
        goBtn.innerHTML = `${icon("compare")}开始对比`;
      }
    });
  }
}

function renderCompareResult(el, p, otherName, data) {
  const a = data.a;
  const b = data.b;
  const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
  el.innerHTML = `
    <div style="display:flex; justify-content:center">
      <canvas class="cie-canvas" id="cmp-canvas" style="max-width:none; width:min(100%, 460px)"></canvas>
    </div>
    <div class="cie-legend" style="margin-top:10px; justify-content:center">
      <span><i class="dot" style="background:var(--indigo)"></i>${esc(p.name)}</span>
      <span><i class="dot" style="background:var(--orange)"></i>${esc(otherName)}</span>
      <span><i class="dot" style="background:var(--teal)"></i>xy 交集</span>
    </div>
    <dl class="kv" style="margin-top:12px">
      <dt>xy 包络面积占比</dt>
      <dd>交集 / A = <b>${pct(data.area_inter_over_a)}</b> · 交集 / B = <b>${pct(data.area_inter_over_b)}</b></dd>
      <dt>采样点归属</dt>
      <dd>A 有 ${data.a_in_b.length}/${a.sampled} 点落入 B 包络 · B 有 ${data.b_in_a.length}/${b.sampled} 点落入 A 包络</dd>
      <dt>色域体积（Lab 包围盒）</dt>
      <dd>A ${formatVolume(a.volume)} · B ${formatVolume(b.volume)}</dd>
      <dt>sRGB 覆盖率</dt>
      <dd>A ${(a.coverage_srgb * 100).toFixed(0)}% · B ${(b.coverage_srgb * 100).toFixed(0)}%</dd>
    </dl>
  `;
  fetchSpectrum().catch(() => null).then((spectrum) => {
    const series = [
      { points: SRGB_XY, color: "var(--accent)", fill: true, fillAlpha: 0.13 },
      // A/B 各拆两个系列：凸包填充多边形（仅顶点）+ 采样点云
      {
        points: a.hull.map((i) => a.xy_points[i]), hull: a.hull.map((_, i) => i),
        color: "var(--indigo)", fill: true, fillAlpha: 0.16, hullWidth: 2, hullAlpha: 0.16,
      },
      { points: a.xy_points, color: "var(--indigo)", dotAlpha: 0.3, pointSize: 1.4 },
      {
        points: b.hull.map((i) => b.xy_points[i]), hull: b.hull.map((_, i) => i),
        color: "var(--orange)", fill: true, fillAlpha: 0.16, hullWidth: 2, hullAlpha: 0.16,
      },
      { points: b.xy_points, color: "var(--orange)", dotAlpha: 0.3, pointSize: 1.4 },
    ];
    if (data.inter_poly && data.inter_poly.length >= 3) {
      series.push({
        points: data.inter_poly, color: "var(--teal)",
        fill: true, fillAlpha: 0.42, hullWidth: 1.4,
      });
    }
    drawCie(el.querySelector("#cmp-canvas"), series, { spectrum });
  });
}

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

    ${p.compensation && p.compensation.n > 0 ? compensationCard(p) : ""}

    <div class="card gamut-card">
      <div class="gamut-card-head">
        <div class="card-title">${icon("target")}工艺色域</div>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
          <div class="cie-legend">
            <span><i class="dot" style="background:linear-gradient(90deg,#4400ff,#00ff00,#ff0000)"></i>光谱轨迹</span>
            <span><i class="dot legend-srgb"></i>sRGB</span>
            <span><i class="dot legend-proc"></i>工艺凸包 · 采样点按 L* 着色</span>
          </div>
          ${p.icc_meta.device_space === "CMYK"
            ? `<button class="btn btn-secondary btn-sm" id="btn-calibration">${icon("printer")}打印校准</button>`
            : ""}
          <button class="btn btn-secondary btn-sm" id="btn-testform">${icon("doc")}生成验证样张</button>
          <button class="btn btn-secondary btn-sm" id="btn-compare">${icon("compare")}色域对比</button>
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

  const compResetBtn = view.querySelector("#btn-comp-reset");
  if (compResetBtn) {
    compResetBtn.addEventListener("click", async () => {
      const ok = await confirmDanger({
        title: "重置补偿模型？",
        message: "累积的墨量偏置将被清除，需重新通过实测验证沉淀。",
        confirmText: "重置",
      });
      if (!ok) return;
      try {
        await apiDelete(`/api/processes/${p.id}/compensation`);
        toast("补偿模型已重置", "ok");
        reload();
      } catch (e) {
        toast(e.message, "err");
      }
    });
  }

  view.querySelector(".btn-back").addEventListener("click", () => (location.hash = "#/processes"));
  view.querySelector("#btn-use").addEventListener("click", () => (location.hash = `#/recipes?process=${p.id}`));
  view.querySelector("#btn-edit").addEventListener("click", () =>
    openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
  );
  view.querySelector("#btn-replace").addEventListener("click", () =>
    openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
  );
  view.querySelector("#btn-compare").addEventListener("click", () => openCompareModal(view, p));
  view.querySelector("#btn-testform").addEventListener("click", () => openTestformModal(view, p));
  const calBtn = view.querySelector("#btn-calibration");
  if (calBtn) calBtn.addEventListener("click", () => openCalibrationModal(view, p));
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
