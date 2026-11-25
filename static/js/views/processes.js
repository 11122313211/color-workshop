/* 工艺库视图（V0.3 重构）：
 * 列表 —— 搜索/筛选/排序工具栏 + 指标卡片（体积/覆盖率/达标率，summary=1 聚合零逐卡请求）
 * 详情 —— 概览指标 + 2D/3D 色域主区（gamut-view 组件）+ ICC 元数据（指纹）+ 配方历史
 * 编辑 —— 新建/编辑合一模态、ICC 替换（sha 变更提示）、批量上传队列
 */

import {
  apiGet, apiPatch, apiDelete, apiUploadICC, apiReplaceICC, fileToArrayBuffer,
  apiPostRaw, apiPost, downloadDataUri,
} from "../api.js";
import { icon } from "../icons.js";
import {
  toast, emptyState, errorState, skeletonCards, esc, downloadText,
  labToHex, sparkline, openModal, confirmDanger, drawCie, fetchSpectrum,
} from "../components.js";
import { createGamutView, SRGB_XY } from "../gamut-view.js";
import { updateNavCount } from "./shell.js";

/* ---------- 列表页 ---------- */

const listState = { search: "", filter: "all", sort: "created", cache: [], includeArchived: false };
let listReqSeq = 0;

export async function renderProcessesView(view, params = {}) {
  const isActive = params.isActive || (() => true);
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
        <input class="input" id="proc-search" placeholder="搜索名称 / 纸张 / 墨水…" aria-label="搜索工艺" value="${esc(listState.search)}">
      </div>
      <div class="seg" data-role="filter" aria-label="按色彩空间筛选">
        ${[["all", "全部"], ["CMYK", "CMYK"], ["RGB", "RGB"], ["GRAY", "GRAY"]].map(([v, t]) =>
          `<button data-f="${v}" class="${listState.filter === v ? "active" : ""}">${t}</button>`
        ).join("")}
      </div>
      <div class="seg" data-role="sort" aria-label="排序">
        ${[["created", "最新"], ["volume", "体积"], ["pass", "达标"]].map(([v, t]) =>
          `<button data-s="${v}" class="${listState.sort === v ? "active" : ""}">${t}</button>`
        ).join("")}
      </div>
      <button class="archive-toggle" id="proc-show-archived" type="button" aria-pressed="false"
        title="显示已归档工艺（只读，可恢复或永久删除）">
        <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
        <span>显示已归档</span>
      </button>
    </div>
    <div id="process-list">${skeletonCards(4)}</div>
  `;

  const archivedToggle = view.querySelector("#proc-show-archived");
  const syncArchivedToggle = () => {
    archivedToggle.setAttribute("aria-pressed", String(listState.includeArchived));
    archivedToggle.classList.toggle("is-on", listState.includeArchived);
    archivedToggle.title = listState.includeArchived
      ? "隐藏已归档工艺"
      : "显示已归档工艺（只读，可恢复或永久删除）";
  };
  syncArchivedToggle();
  archivedToggle.addEventListener("click", () => {
    listState.includeArchived = !listState.includeArchived;
    syncArchivedToggle();
    loadList(view, isActive);
  });

  view.querySelector("#btn-new").addEventListener("click", () =>
    openProcessEditor(view, { mode: "create", onDone: () => loadList(view, isActive) })
  );

  // 搜索（300ms debounce，纯前端过滤）
  let debounce = 0;
  params.onCleanup?.(() => clearTimeout(debounce));
  view.querySelector("#proc-search").addEventListener("input", (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      if (!isActive()) return;
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

  await loadList(view, isActive, params.signal);
}

async function loadList(view, isActive = () => true, signal) {
  const box = view.querySelector("#process-list");
  if (!box) return;
  const reqId = ++listReqSeq;
  box.innerHTML = `<div class="spinner"></div>`;
  try {
    // summary=1：一次请求带出色域摘要与达标率（消除逐卡 N+1）
    const query = new URLSearchParams({ summary: "1" });
    if (listState.includeArchived) query.set("include_archived", "1");
    const docs = await apiGet(`/api/processes?${query.toString()}`, { signal });
    if (!isActive() || reqId !== listReqSeq) return;
    listState.cache = docs;
    updateNavCount();
    renderCards(view);
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (!isActive() || reqId !== listReqSeq) return;
    box.innerHTML = errorState(e.message);
    box.querySelector(".btn-retry")?.addEventListener("click", () => loadList(view, isActive));
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
      desc: "上传一个 ICC 特性文件即可创建工艺；若工艺已归档，可开启“显示已归档”查看只读记录。",
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
    const open = () => (location.hash = `#/processes/${el.dataset.id}`);
    el.addEventListener("click", open);
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    // 迷你色域凸包缩略图（summary 数据本地绘制，零额外请求）
    drawMiniHull(el.querySelector(".process-mini-cie"), docs.find((d) => d.id === el.dataset.id));
  });
}

function isArchived(p) {
  return Boolean(p && (p.archived_at || p.is_archived || p.archived === true));
}

function formatArchivedWhen(value) {
  if (!value) return "未记录时间";
  return String(value).replace("T", " ").replace(/(\.\d+)?Z$/, "");
}

function formatArchiveReason(value) {
  if (!value) return "";
  return value === "manual_archive" ? "手动归档" : String(value);
}

function cardHtml(p) {
  const space = p.icc_meta.device_space;
  const s = p.gamut_summary;
  const archived = isArchived(p);
  return `
  <div class="card process-card ${archived ? "is-archived" : ""}" data-id="${p.id}" tabindex="0" role="button"
    aria-label="打开${archived ? "已归档" : ""}工艺 ${esc(p.name)}">
    <div class="process-head">
      <div class="process-mini-cie"></div>
      <div class="grow-truncate">
        <div class="proc-name truncate">${esc(p.name)}</div>
        <div class="proc-desc truncate">${esc(p.icc_meta.description || p.icc_original_name)}</div>
      </div>
    </div>
    <div class="proc-chips">
      ${archived ? `<span class="badge badge-gray">${icon("stack")}已归档</span>` : ""}
      <span class="badge ${space === "CMYK" ? "badge-blue" : space === "GRAY" ? "badge-gray" : "badge-gray"}">${space} · ${p.icc_meta.channels}通道</span>
      ${p.paper ? `<span class="chip">${esc(p.paper)}</span>` : ""}
      ${p.measurement_condition ? `<span class="chip chip-mono">${esc(p.measurement_condition)}</span>` : ""}
    </div>
    ${archived ? `<div class="proc-archive-meta">
      <span>归档于 ${esc(formatArchivedWhen(p.archived_at))}</span>
      ${p.archive_reason ? `<span class="proc-archive-reason">${esc(formatArchiveReason(p.archive_reason))}</span>` : ""}
    </div>` : ""}
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
  if (isEdit && isArchived(process)) {
    toast("工艺已归档，恢复后才能编辑或替换 ICC", "warn");
    return;
  }
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
      <div class="field-row" style="margin-top:10px">
        <div class="field"><label>测量条件（工艺护照）</label>
          <select class="input" id="pe-condition">
            <option value="">未登记</option>
            <option value="M0">M0 · 未滤紫外</option>
            <option value="M1">M1 · 含紫外（D50）</option>
            <option value="M2">M2 · 排除紫外</option>
          </select>
        </div>
      </div>
      <div class="field" style="margin-top:10px"><label>备注</label><input class="input" id="pe-notes" maxlength="300" placeholder="驱动设置、印刷条件等" value="${isEdit ? esc(process.notes || "") : ""}"></div>
    </div>
    <div data-role="icc-area" style="margin-top:14px">
      ${
        isEdit
          ? `<div class="icc-current">
              <div class="icc-line">当前 ICC：${esc(process.icc_original_name)} · ${(process.icc_size / 1024).toFixed(1)} KB
                <span class="chip chip-mono">${process.icc_sha256.slice(0, 8)}</span></div>
              <div class="dropzone dropzone-sm" id="pe-drop" role="button" tabindex="0" aria-label="拖入新 ICC 文件可替换">
                ${icon("swap")}
                <div style="font-weight:600">拖入新 ICC 文件可替换</div>
                <div style="font-size:12px;margin-top:4px" id="pe-file-label">不更换则保持现状</div>
              </div>
              <div class="icc-diff" id="pe-diff" hidden></div>
            </div>`
          : `<div class="dropzone" id="pe-drop" role="button" tabindex="0" aria-label="点击或拖入 ICC 文件">
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
  const condSelect = modal.querySelector("#pe-condition");
  if (isEdit && process.measurement_condition) condSelect.value = process.measurement_condition;
  let upMode = "single";

  const readForm = () => ({
    name: nameInput.value.trim(),
    paper: modal.querySelector("#pe-paper").value.trim(),
    ink: modal.querySelector("#pe-ink").value.trim(),
    notes: modal.querySelector("#pe-notes").value.trim(),
    measurement_condition: condSelect.value,
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
    const diff = modal.querySelector("#pe-diff");
    if (isEdit && diff) {
      diff.hidden = !iccFile;
      diff.textContent = iccFile
        ? `将替换为 ${iccFile.name}（保存后色域缓存失效，补偿模型重置，原签样需重新确认）`
        : "";
    }
    refreshOk();
  };
  input.addEventListener("change", () => {
    const files = [...input.files];
    if (!isEdit && files.length > 1) {
      openQueue(files);
    } else {
      setFile(files[0] || null);
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
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
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
        q.doc = await apiUploadICC(new Uint8Array(buf), {
          name: q.name || q.file.name,
          filename: q.file.name,
          measurement_condition: readForm().measurement_condition,
        }, {
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
function compensationCard(p, archived = false) {
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
      <button class="btn btn-secondary btn-sm" id="btn-comp-reset"
        ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能重置补偿模型"` : ""}>重置</button>
    </div>`;
}

/* 验证样张模态（V0.5 M3）：双点重复布局的 A4 CMYK 样张，PDF/TIFF 下载 */
async function openTestformModal(view, p) {
  if (isArchived(p)) {
    toast("工艺已归档，恢复后才能生成验证样张", "warn");
    return;
  }
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

/* 校准轮次历史卡（V0.8）：收敛判定 + max ΔE 趋势 + 最近轮次列表 */
function calibrationHistoryCard(hist, archived = false) {
  const conv = hist.convergence || {};
  const convCls = { converged: "badge-green", improving: "badge-blue", worsening: "badge-red", flat: "badge-orange", "n/a": "" }[conv.status] || "";
  const convChip = conv.status && conv.status !== "n/a"
    ? `<span class="badge ${convCls}">${esc(conv.status_label)}</span>`
    : `<span class="chip chip-mono" style="color:var(--label-3)">${esc(conv.status_label || "轮次不足")}</span>`;
  const rows = (hist.rounds || []).slice(0, 5).map((r, i) => `
    <div style="display:flex; align-items:center; gap:10px; padding:6px 0; border-top:${i ? "1px solid var(--separator)" : "none"}">
      <span class="chip chip-mono">第 ${(hist.rounds.length - i).toLocaleString()} 轮</span>
      ${r.overall === "ok" ? `<span class="badge badge-green">达标</span>`
        : r.overall === "suggest" ? `<span class="badge badge-orange">建议校准</span>`
        : `<span class="badge badge-red">需校准</span>`}
      <span class="swatch-meta chip-mono">max ${r.summary?.max_de_expected ?? "-"} · avg ${r.summary?.avg_de_expected ?? "-"}</span>
      <span style="flex:1"></span>
      <span class="swatch-meta">${esc((r.created_at || "").replace("T", " "))}</span>
    </div>`).join("");
  return `
    <div class="card" style="padding:12px 16px; margin-top:12px">
      <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
        <span class="card-title" style="margin:0; display:inline-flex; align-items:center; gap:6px">
          ${icon("printer")}校准历史 <span class="chip">${hist.round_no} 轮</span>
        </span>
        ${convChip}
        <span style="flex:1"></span>
        ${sparkline(hist.trend || [], { width: 130, height: 26, tolerance: 3.5 })}
        <button class="btn btn-secondary btn-sm" data-cal-open type="button"
          ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能打印校准"` : ""}>${icon("printer")}打印校准</button>
      </div>
      <div style="margin-top:6px">${rows}</div>
    </div>`;
}

/* ---------- 工艺护照（V0.10）：版本化色彩证据 ---------- */

const PASSPORT_STATUS = {
  valid: ["有效", "badge-green"],
  incomplete: ["证据不完整", "badge-orange"],
  stale: ["已失效", "badge-red"],
  invalid: ["无效", "badge-red"],
};

const SPECTRAL_RISK = {
  low: ["低风险", "badge-green"],
  medium: ["中风险", "badge-orange"],
  high: ["高风险", "badge-red"],
  none: ["未评估", "badge-gray"],
};

function passportBadge(status, label = "") {
  const [defaultLabel, cls] = PASSPORT_STATUS[status] || ["未生成", "badge-gray"];
  return `<span class="badge ${cls}">${icon(status === "valid" ? "check" : status === "incomplete" ? "warn" : "xmark")}${esc(label || defaultLabel)}</span>`;
}

function spectralRiskBadge(risk) {
  const [label, cls] = SPECTRAL_RISK[risk] || SPECTRAL_RISK.none;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function passportCard(p, latest) {
  const archived = isArchived(p);
  if (!latest) {
    return `<div class="card passport-card" style="padding:14px 16px; margin-top:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="card-title" style="margin:0">${icon("doc")}工艺护照</span>
        <span class="badge badge-gray">未生成</span>
        <span style="flex:1"></span>
        <button class="btn btn-sm" data-pp="refresh"
          ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能生成护照"` : ""}>${icon("plus")}生成护照</button>
      </div>
      <p class="swatch-meta" style="margin-top:8px">把 ICC、补偿、校准轮次、可信测量、配方统计与光谱风险固化为不可变版本；签样可通过 hash 引用护照。</p>
    </div>`;
  }
  const evidence = latest.evidence || {};
  const measurements = evidence.measurements || {};
  const calibration = evidence.calibration || {};
  const recipes = evidence.recipes || {};
  const spectral = evidence.spectral || {};
  const warnings = latest.warnings || [];
  const reasons = latest.status_reasons || [];
  const visibleWarnings = [...reasons, ...warnings].slice(0, 3);
  const warningHtml = visibleWarnings.length
    ? `<div class="issue-list" style="margin-top:10px">${visibleWarnings.map((w) => `
        <div class="issue ${w.level === "error" ? "issue-error" : w.level === "info" ? "issue-info" : "issue-warn"}">
          ${icon(w.level === "error" ? "xmark" : w.level === "info" ? "info" : "warn")}<span>${esc(w.message)}</span>
        </div>`).join("")}</div>`
    : "";
  return `<div class="card passport-card" style="margin-top:12px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="card-title" style="margin:0">${icon("doc")}工艺护照</span>
      ${passportBadge(latest.status, latest.status_label)}
      <span class="chip chip-mono">v${latest.version}</span>
      <span class="chip chip-mono" title="${esc(latest.passport_hash)}">#${esc((latest.passport_hash || "").slice(0, 12))}</span>
      <span style="flex:1"></span>
      <button class="btn btn-secondary btn-sm" data-pp="versions">${icon("stack")}版本历史</button>
      <button class="btn btn-sm" data-pp="refresh"
        ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能生成或刷新护照"` : ""}>${icon("refresh")}生成 / 刷新</button>
      <a class="btn btn-secondary btn-sm" href="/api/processes/${encodeURIComponent(p.id)}/passport/${latest.version}/export?format=json" download>${icon("download")}JSON</a>
      <a class="btn btn-secondary btn-sm" href="/api/processes/${encodeURIComponent(p.id)}/passport/${latest.version}/export?format=md" download>${icon("download")}MD</a>
      <button class="btn btn-secondary btn-sm" data-pp="detail">查看详情</button>
    </div>
    <div class="passport-summary-row">
      <span>可信测量 <b>${measurements.verified_records ?? 0}</b> 条</span>
      <span>校准 <b>${calibration.rounds ?? 0}</b> 轮</span>
      <span>配方 <b>${recipes.total ?? 0}</b> 条</span>
      <span>光谱 <b>${spectral.spectral_patches ?? 0}</b> 块</span>
      <span>光谱风险 ${spectralRiskBadge(spectral.max_risk)}</span>
    </div>
    <div class="swatch-meta" style="margin-top:8px">生成于 ${esc(latest.generated_at || "-")} · 快照内容不变时重复生成不会新增版本</div>
    ${warningHtml}
    <p class="swatch-meta" style="margin-top:8px">${esc(spectral.disclaimer || "工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI")}</p>
  </div>`;
}

function openPassportDetail(latest) {
  if (!latest) return;
  const evidence = latest.evidence || {};
  const icc = evidence.icc || {};
  const comp = evidence.compensation || {};
  const cal = evidence.calibration || {};
  const meas = evidence.measurements || {};
  const recipes = evidence.recipes || {};
  const spectral = evidence.spectral || {};
  const warnings = latest.warnings || [];
  const reasons = latest.status_reasons || [];
  const m = openModal(`
    <h3 class="card-title">${icon("doc")}工艺护照 · ${esc((latest.process || {}).name || "")}</h3>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 14px">
      ${passportBadge(latest.status, latest.status_label)}
      <span class="chip chip-mono">v${latest.version}</span>
      <span class="swatch-meta">${esc(latest.generated_at || "")}</span>
    </div>
    <dl class="kv">
      <dt>Passport hash</dt><dd>${esc(latest.passport_hash)}</dd>
      <dt>测量条件</dt><dd>${esc((latest.process || {}).measurement_condition || "未登记")}</dd>
      <dt>ICC</dt><dd>${esc(icc.sha256 || "")} · ${esc(icc.device_space || "")} ${icc.channels || ""}CH</dd>
      <dt>补偿模型</dt><dd>n=${comp.n ?? 0} · 匹配当前 ICC：${comp.matches_current_icc ? "是" : "否"}</dd>
      <dt>校准轮次</dt><dd>${cal.rounds ?? 0} 轮${cal.latest_round_id ? ` · ${esc(cal.latest_round_id)}` : ""}</dd>
      <dt>可信测量</dt><dd>${meas.verified_records ?? 0} 条 / ${meas.sessions ?? 0} 会话</dd>
      <dt>配方历史</dt><dd>${recipes.total ?? 0} 条 · 达标率 ${recipes.pass_rate == null ? "-" : Math.round(recipes.pass_rate * 100) + "%"}</dd>
      <dt>光谱覆盖</dt><dd>${spectral.spectral_patches ?? 0} 块 · ${spectral.wavelength_min ?? "-"}–${spectral.wavelength_max ?? "-"} nm · 可比较会话 ${spectral.comparable_sessions ?? 0}</dd>
      <dt>最高光谱风险</dt><dd>${esc(spectral.max_risk || "none")}${spectral.worst_condition ? ` · ${esc(spectral.worst_condition)}` : ""}${spectral.max_delta_e != null ? ` · ΔE ${spectral.max_delta_e}` : ""}</dd>
    </dl>
    ${reasons.length ? `<div class="section-title" style="margin:18px 2px 8px">状态原因</div>
      <div class="issue-list">${reasons.map((w) => `<div class="issue issue-error">${icon("xmark")}<span>${esc(w.message)}</span></div>`).join("")}</div>` : ""}
    ${warnings.length ? `<div class="section-title" style="margin:18px 2px 8px">警告</div>
      <div class="issue-list">${warnings.map((w) => `<div class="issue ${w.level === "error" ? "issue-error" : w.level === "info" ? "issue-info" : "issue-warn"}">${icon(w.level === "error" ? "xmark" : w.level === "info" ? "info" : "warn")}<span>${esc(w.message)}</span></div>`).join("")}</div>` : ""}
    <div class="issue issue-info" style="margin-top:14px">${icon("info")}<span>${esc(spectral.disclaimer || "工程同色异谱风险指标，不等同 CIE 15 特殊同色异谱指数 MI")}</span></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-secondary" data-close>关闭</button>
    </div>
  `);
  m.modal.style.width = "min(700px, 100%)";
  m.modal.querySelector("[data-close]").addEventListener("click", m.close);
}

function openPassportVersions(p) {
  const m = openModal(`
    <h3 class="card-title">${icon("stack")}护照版本历史 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <p class="swatch-meta" style="margin-top:4px">版本快照只读；签样引用可帮助判断旧版本仍在哪些放行记录中使用。</p>
    <div id="passport-versions-body" style="margin-top:14px"><div class="spinner"></div></div>
  `);
  m.modal.style.width = "min(860px, 100%)";
  const body = m.modal.querySelector("#passport-versions-body");

  const openVersion = async (version, row) => {
    if (row?.classList.contains("is-loading")) return;
    row?.classList.add("is-loading");
    try {
      const passport = await apiGet(
        `/api/processes/${encodeURIComponent(p.id)}/passport/${encodeURIComponent(version.version)}`
      );
      openPassportDetail(passport);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      row?.classList.remove("is-loading");
    }
  };

  const load = async () => {
    body.innerHTML = `<div style="display:flex;align-items:center;gap:8px"><span class="spinner"></span><span class="swatch-meta">读取版本历史…</span></div>`;
    try {
      const versions = await apiGet(`/api/processes/${encodeURIComponent(p.id)}/passport/versions`);
      if (!m.modal.isConnected) return;
      if (!versions.length) {
        body.innerHTML = emptyState({
          iconName: "doc", title: "暂无护照版本",
          desc: "生成工艺护照后，每次证据变化会沉淀为新的不可变版本。",
        });
        return;
      }
      const stateLabel = { draft: "草稿", pending: "待确认", approved: "已批准", rejected: "已驳回", locked: "已锁定" };
      const rows = versions.map((v) => {
        const refs = v.referenced_by_signoffs || [];
        const refCount = v.referenced_count ?? refs.length;
        const warningCount = Array.isArray(v.warnings) ? v.warnings.length : Number(v.warnings || 0);
        const refHtml = refs.length
          ? refs.map((ref) => `<span class="version-ref">${esc(ref.title || ref.id || "未命名签样")}
              <span class="chip">${esc(stateLabel[ref.state] || ref.state || "未知")}</span></span>`).join("")
          : (refCount > 0 ? `<span class="swatch-meta">${refCount} 条签样引用</span>` : "—");
        return `<tr tabindex="0" role="button" class="passport-version-row"
          data-version="${v.version}" aria-label="查看护照 v${v.version} 只读详情">
          <td data-label="版本"><span class="chip chip-mono">v${v.version}</span>${v.is_latest ? `<span class="badge badge-blue">当前</span>` : ""}</td>
          <td data-label="状态">${passportBadge(v.status, v.status_label)}</td>
          <td data-label="Hash"><span class="version-hash chip chip-mono" title="${esc(v.passport_hash || "")}">${esc(v.passport_hash || "—")}</span></td>
          <td data-label="生成时间" class="swatch-meta">${esc(formatArchivedWhen(v.generated_at))}</td>
          <td data-label="警告">${warningCount ? `<span class="badge badge-orange">${warningCount} 条</span>` : `<span class="swatch-meta">0</span>`}</td>
          <td data-label="签样引用">${refHtml}</td>
        </tr>`;
      }).join("");
      body.innerHTML = `
        <div class="passport-version-wrap">
          <table class="version-table">
            <thead><tr><th>版本</th><th>状态</th><th>Hash</th><th>生成时间</th><th>警告</th><th>签样引用</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <p class="swatch-meta" style="margin-top:10px">点击任意版本行打开只读详情；归档工艺的历史版本同样可查看。</p>
        <div style="display:flex;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-secondary" data-close>关闭</button>
        </div>`;
      body.querySelectorAll("[data-version]").forEach((row) => {
        const version = versions.find((v) => String(v.version) === row.dataset.version);
        row.addEventListener("click", () => openVersion(version, row));
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openVersion(version, row);
          }
        });
      });
      body.querySelector("[data-close]").addEventListener("click", m.close);
    } catch (e) {
      body.innerHTML = `<div class="issue issue-error">${icon("xmark")}<span>${esc(e.message)}</span></div>
        <div style="display:flex;justify-content:flex-end;margin-top:14px">
          <button class="btn btn-secondary" id="pp-versions-retry">重试</button>
        </div>`;
      body.querySelector("#pp-versions-retry").addEventListener("click", load);
    }
  };

  load();
}

async function renderPassportSlot(view, p, isActive = () => true, signal) {
  const slot = view.querySelector("#passport-slot");
  if (!slot) return;
  const archived = isArchived(p);
  let latest = null;
  try {
    latest = await apiGet(`/api/processes/${encodeURIComponent(p.id)}/passport`, { signal });
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (!String(e.message).includes("尚未生成")) {
      if (isActive() && slot.isConnected) {
        slot.innerHTML = `<div class="card passport-card" style="margin-top:12px"><div class="issue issue-warn">${icon("warn")}<span>${esc(e.message)}</span></div></div>`;
      }
      return;
    }
  }
  if (!isActive() || !slot.isConnected) return;
  slot.innerHTML = passportCard(p, latest);
  const card = slot.querySelector(".passport-card");
  if (!card) return;
  const refreshBtn = card.querySelector('[data-pp="refresh"]');
  if (refreshBtn && !archived) refreshBtn.addEventListener("click", async (event) => {
    if (isArchived(p)) return;
    const btn = event.currentTarget;
    btn.disabled = true;
    try {
      const next = await apiPost(`/api/processes/${encodeURIComponent(p.id)}/passport`, {});
      toast(next.created ? `护照 v${next.version} 已生成` : `内容未变，沿用护照 v${next.version}`, "ok");
      await renderPassportSlot(view, p, isActive, signal);
    } catch (e) {
      toast(e.message, "err");
      if (btn.isConnected) btn.disabled = false;
    }
  });
  card.querySelector('[data-pp="detail"]')?.addEventListener("click", () => openPassportDetail(latest));
  card.querySelector('[data-pp="versions"]')?.addEventListener("click", () => openPassportVersions(p));
}

/* ---------- 签样与放行（V0.9 · P0-3）：draft→pending→approved→locked ---------- */


const SIGNOFF_STATE = {
  draft: ["草稿", "badge badge-gray"],
  pending: ["待确认", "badge badge-orange"],
  approved: ["已批准", "badge badge-green"],
  rejected: ["已驳回", "badge badge-red"],
  locked: ["已锁定", "badge badge-blue"],
};

const SIGNOFF_ACTION = {
  submit: { title: "提交签样", desc: "提交后等待客户/主管批准。", confirm: "提交", icon: "check" },
  approve: { title: "批准签样", desc: "批准后该工艺可作为下游放行依据。", confirm: "批准", icon: "check" },
  reject: { title: "驳回签样", desc: "请填写驳回理由，便于返工后重新提交。", confirm: "驳回", icon: "xmark" },
  withdraw: { title: "撤回签样", desc: "签样回到草稿状态，可修改依据后重新提交。", confirm: "撤回", icon: "refresh" },
  lock: { title: "锁定签样", desc: "锁定后记录不可变更，并生成放行令牌。", confirm: "锁定", icon: "check" },
};

function evidenceText(ev = {}) {
  const bits = [];
  if (ev.passport_hash) bits.push(`工艺护照 #${esc(ev.passport_hash.slice(0, 10))}`);
  if (ev.calibration_session_id) bits.push(`校准会话 ${esc(ev.calibration_session_id)}`);
  if (ev.measurement_session_id) bits.push(`测量会话 ${esc(ev.measurement_session_id)}`);
  if (ev.evaluation_id) bits.push(`主观评价 ${esc(ev.evaluation_id)}`);
  if (ev.file_name) bits.push(`文件 ${esc(ev.file_name)}`);
  if (ev.testform) bits.push("测量样张");
  return bits.length ? bits.join(" · ") : "未关联依据";
}

function signoffActions(latest, archived = false) {
  if (archived) {
    return `<span class="readonly-pill">${icon("info")}只读 · 恢复后可继续签样</span>`;
  }
  if (!latest) return `<button class="btn btn-sm" data-sg="create">${icon("plus")}新建签样</button>`;
  const state = latest.state;
  if (state === "draft") {
    return `<button class="btn btn-sm" data-sg="submit">${icon("check")}提交确认</button>
      <button class="btn btn-secondary btn-sm" data-sg="delete">${icon("trash")}删除</button>`;
  }
  if (state === "pending") {
    return `<button class="btn btn-sm" data-sg="approve">${icon("check")}批准</button>
      <button class="btn btn-secondary btn-sm" data-sg="reject">${icon("xmark")}驳回</button>
      <button class="btn btn-ghost btn-sm" data-sg="withdraw">撤回</button>`;
  }
  if (state === "approved") {
    return `<button class="btn btn-sm" data-sg="lock">${icon("check")}锁定签样</button>
      <button class="btn btn-secondary btn-sm" data-sg="create">${icon("plus")}重新签样</button>`;
  }
  if (state === "rejected") {
    return `<button class="btn btn-sm" data-sg="submit">${icon("refresh")}重新提交</button>`;
  }
  if (state === "locked") {
    return `<button class="btn btn-secondary btn-sm" data-sg="manifest">${icon("download")}交接清单</button>
      <button class="btn btn-secondary btn-sm" data-sg="create">${icon("plus")}重新签样</button>`;
  }
  return "";
}

function signoffCard(rows, gate, archived = false) {
  const latest = rows[0];
  const [label, cls] = latest
    ? (SIGNOFF_STATE[latest.state] || ["未知", "badge badge-gray"])
    : ["未签样", "badge badge-gray"];
  const gateReason = String(gate.reason || "");
  const archivedGate = gateReason === "process_archived";
  const passportChanged = gateReason === "passport_changed";
  const gateLine = archivedGate
    ? `<div class="issue issue-info archived-gate">${icon("info")}<span>工艺已归档，签样与放行门禁为只读；恢复工艺后可继续签样、提交或放行。</span></div>`
    : gate.allowed
      ? `<span style="color:var(--green);display:inline-flex;align-items:center;gap:6px">${icon("check")}${esc(gate.message)}</span>`
      : `<span style="color:var(--orange);display:inline-flex;align-items:center;gap:6px">${icon("warn")}${esc(gate.message)}</span>`;
  const passportGate = gateReason.startsWith("passport_");
  const gateExtra = passportGate
    ? `<div class="swatch-meta signoff-passport-gate" style="margin-top:6px"
        ${passportChanged ? `role="alert" aria-live="assertive"` : ""}>签样护照 <span class="chip chip-mono">${esc((gate.snapshot_passport || "").slice(0, 10) || "—")}</span>
        当前最新护照 <span class="chip chip-mono">${esc((gate.current_passport || "").slice(0, 10) || "—")}</span>
        ${gate.passport_status ? passportBadge(gate.passport_status, gate.passport_status_label) : ""}</div>`
    : "";
  const detail = latest ? `
    <dl class="kv" style="margin-top:10px">
      <dt>标题 / 工单</dt><dd>${esc(latest.title)}${latest.job_no ? ` · ${esc(latest.job_no)}` : ""}</dd>
      <dt>依据</dt><dd>${evidenceText(latest.evidence)}</dd>
      <dt>快照 ICC</dt><dd><span class="chip chip-mono">${(latest.snapshot.icc_sha256 || "").slice(0, 8)}</span></dd>
      ${latest.snapshot.passport_hash ? `<dt>关联护照</dt><dd><span class="chip chip-mono">v${latest.snapshot.passport_version ?? "?"} · #${esc(latest.snapshot.passport_hash.slice(0, 10))}</span></dd>` : ""}
      ${latest.decision ? `<dt>${latest.decision.action === "approve" ? "批准" : "驳回"}</dt><dd>${esc(latest.decision.by)} · ${esc(latest.decision.at)}${latest.decision.note ? ` · ${esc(latest.decision.note)}` : ""}</dd>` : ""}
      <dt>最近更新</dt><dd>${esc(latest.updated_at)}</dd>
    </dl>`
    : `<div style="font-size:12px;color:var(--label-2);margin-top:8px">签样记录客户确认与版本依据；未签样时下游交接清单会被门禁拦截。</div>`;
  return `
    <div class="card" style="padding:12px 16px; margin-top:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="card-title" style="margin:0;display:inline-flex;align-items:center;gap:6px">
          ${icon("check")}签样放行 <span class="${cls}">${label}</span>
        </span>
        <span style="flex:1"></span>
        <div style="display:flex;gap:8px;flex-wrap:wrap">${signoffActions(latest, archived)}</div>
      </div>
      <div style="margin-top:8px;font-size:13px">${gateLine}</div>
      ${gateExtra}
      ${detail}
    </div>`;
}

function openSignoffCreate(p, onDone) {
  if (isArchived(p)) {
    toast("工艺已归档，恢复后才能新建签样", "warn");
    return;
  }
  const m = openModal(`
    <h3 class="card-title">${icon("check")}新建签样 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <div class="field"><label>签样标题</label><input class="input" id="sg-title" maxlength="80" value="${esc(p.name)} 签样"></div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>工单号</label><input class="input" id="sg-job" maxlength="60" placeholder="可选"></div>
      <div class="field"><label>创建人</label><input class="input" id="sg-by" maxlength="40" placeholder="姓名"></div>
    </div>
    <div class="field" style="margin-top:10px"><label>样张 / 文件标识</label><input class="input" id="sg-file" maxlength="120" placeholder="如 封面-v3.pdf、校准样张第 2 轮"></div>
    <div class="field" style="margin-top:10px"><label>关联工艺护照（可选）</label>
      <select class="input" id="sg-passport"><option value="">不关联（沿用 V0.9 ICC 指纹门禁）</option></select>
      <p class="swatch-meta" style="margin-top:4px">关联后签样会固化护照 hash；最新护照变化时门禁返回 <b>passport_changed</b>，旧签样失效。</p>
    </div>
    <div class="field" style="margin-top:10px"><label>关联测量会话（可选）</label><input class="input" id="sg-session" placeholder="sess_xxx"></div>
    <div class="field" style="margin-top:10px"><label>关联主观评价（可选）</label><input class="input" id="sg-eval" placeholder="ev_xxx"></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="sg-create">${icon("plus")}创建草稿</button>
    </div>
  `);
  apiGet(`/api/processes/${encodeURIComponent(p.id)}/passport`).then((pp) => {
    const sel = m.modal.querySelector("#sg-passport");
    if (!sel || !pp) return;
    const opt = document.createElement("option");
    const eligible = pp.status === "valid";
    opt.value = pp.id;
    opt.dataset.hash = pp.passport_hash;
    opt.disabled = !eligible;
    opt.textContent = `v${pp.version} · #${pp.passport_hash.slice(0, 10)} · ${pp.status_label}${eligible ? "" : " · 不可关联"}`;
    sel.appendChild(opt);
  }).catch(() => {});
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#sg-create").addEventListener("click", async () => {
    const evidence = {};
    const file = m.modal.querySelector("#sg-file").value.trim();
    const session = m.modal.querySelector("#sg-session").value.trim();
    const evalId = m.modal.querySelector("#sg-eval").value.trim();
    const passportSel = m.modal.querySelector("#sg-passport");
    const passportId = passportSel.value;
    const passportHash = passportSel.selectedOptions[0]?.dataset.hash || "";
    if (file) evidence.file_name = file;
    if (session) evidence.measurement_session_id = session;
    if (evalId) evidence.evaluation_id = evalId;
    if (!Object.keys(evidence).length && !passportId) {
      return toast("请至少填写一项依据（文件 / 会话 / 评价 / 工艺护照）", "warn");
    }
    try {
      await apiPost("/api/signoffs", {
        process_id: p.id,
        title: m.modal.querySelector("#sg-title").value.trim(),
        job_no: m.modal.querySelector("#sg-job").value.trim(),
        created_by: m.modal.querySelector("#sg-by").value.trim(),
        evidence,
        passport_id: passportId,
        passport_hash: passportHash,
      });
      toast("签样草稿已创建", "ok");
      m.close();
      onDone();
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

function openSignoffAction(action, latest, onDone) {
  const meta = SIGNOFF_ACTION[action];
  if (!meta) return;
  const needReason = action === "reject";
  const m = openModal(`
    <h3 class="card-title">${icon(meta.icon)}${meta.title}</h3>
    <div style="font-size:12px;color:var(--label-2);margin:6px 0 12px">${meta.desc}</div>
    <div class="field"><label>操作人 *</label><input class="input" id="sg-actor" maxlength="40" placeholder="姓名"></div>
    <div class="field" style="margin-top:10px"><label>${needReason ? "驳回理由 *" : "备注"}</label><textarea class="input" id="sg-note" rows="3" maxlength="300"></textarea></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn ${needReason ? "btn-danger" : ""}" id="sg-do">${meta.confirm}</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#sg-do").addEventListener("click", async () => {
    const actor = m.modal.querySelector("#sg-actor").value.trim();
    const note = m.modal.querySelector("#sg-note").value.trim();
    if (!actor) return toast("请填写操作人", "warn");
    if (needReason && !note) return toast("驳回必须填写理由", "warn");
    try {
      await apiPost(`/api/signoffs/${latest.id}/${action}`, { actor, note });
      toast(`签样已${meta.confirm}`, "ok");
      m.close();
      onDone();
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

async function downloadSignoffManifest(p) {
  try {
    const data = await apiGet(`/api/signoffs/release-manifest?process_id=${encodeURIComponent(p.id)}`);
    const name = data.signoff.job_no || data.signoff.id;
    downloadDataUri(
      "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(data, null, 2)),
      `签样交接-${p.name}-${name}.json`,
    );
    toast("交接清单已生成", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

/* 打印校准向导（V0.7）：① 生成校准版+参考表 → ② 导入测量+双基准报告 → ③ 应用墨量建议+导出。
 * 双基准：工艺 ICC 预期（模型误差）+ ISO 12647-2 固体参考（绝对达标，仅涂布纸）。
 * 校准建议一键写入补偿模型（预测联动）；校正曲线/特性数据导出供外部专业软件生成 ICC。
 * V0.8：轮次化——apply 落库轮次，报告自动对比上一轮（改善/持平/恶化），收敛判定。 */
async function openCalibrationModal(view, p) {
  if (isArchived(p)) {
    toast("工艺已归档，恢复后才能打印校准", "warn");
    return;
  }
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
      .map((n, i) => `<span style="${state.step === i + 1 ? "color:var(--label); font-weight:700" : "color:var(--label-3)"}">${n}</span>`)
      .join(`<span style="color:var(--label-3)">→</span>`);
  };

  const renderStep1 = () => {
    state.step = 1;
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
        $(".cal-ref").addEventListener("click", () => downloadText(
          ref.reference_cgats || ref.cgats_reference || "",
          `calibration-reference-${p.name}.txt`,
          "text/plain;charset=utf-8"));
        $(".cal-next1").addEventListener("click", renderStep2);
      } catch (e) {
        $("#cal-gen-result").innerHTML = `<div class="gamut-error">${esc(e.message)}</div>`;
      }
      btn.disabled = false;
    });
  };

  const renderStep2 = () => {
    state.step = 2;
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
    // 一次性绑定下一步，避免多次导入后叠加回调
    $(".cal-next2").addEventListener("click", () => {
      if (state.report && state.report.overall !== "n/a") renderStep3();
    });

    const renderReport = (rep) => {
      const sugChips = (rep.ink_suggestions || []).map((s) => {
        if (!s.available) return `<span class="chip chip-mono" style="color:var(--label-3)">${s.channel} 缺固体实测</span>`;
        if (Math.abs(s.delta_pct) < 0.5) return `<span class="chip chip-mono">${s.channel} ±0%</span>`;
        const warn = s.beyond_range ? ` ⚠超出墨量修正范围` : "";
        return `<span class="chip chip-mono">${s.channel} ${s.delta_pct > 0 ? "+" : ""}${s.delta_pct}%${warn}</span>`;
      }).join(" ");
      // V0.8：轮次对比卡（较上轮）
      const cmp = rep.prev_round_compare;
      const cmpCard = cmp ? `
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:10px; padding:8px 12px; border:1px solid var(--separator); border-radius:10px">
          <span class="swatch-meta">较上一轮（${esc((cmp.prev_created_at || "").replace("T", " "))}）</span>
          <span class="badge ${cmp.verdict === "improved" ? "badge-green" : cmp.verdict === "worsened" ? "badge-red" : "badge-orange"}">${esc(cmp.verdict_label)}</span>
          <span class="swatch-meta chip-mono">max ΔE ${cmp.prev_max_de_expected ?? "-"} → ${rep.summary.max_de_expected ?? "-"}${cmp.d_max_de_expected !== null ? `（${cmp.d_max_de_expected > 0 ? "+" : ""}${cmp.d_max_de_expected}）` : ""}</span>
        </div>` : "";
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
        ${cmpCard}
        ${sugChips ? `<div style="margin-top:8px">${sugChips}</div>` : ""}
        <div style="max-height:300px; overflow:auto; margin-top:10px">
          <table class="lab-table" style="width:100%">
            <thead><tr><th>色块</th><th>实测</th><th>预期</th><th>ΔE ICC</th><th>ΔE ISO</th><th>ISO 判定</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
      const canProceed = rep.overall !== "n/a";
      $(".cal-next2").disabled = !canProceed;
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
    state.step = 3;
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
    $(".cal-csv").addEventListener("click", () => downloadText(
      "\ufeff" + (state.report.exports.curves_csv || ""),
      `calibration-curves-${p.name}.csv`,
      "text/csv;charset=utf-8"));
    $(".cal-char").addEventListener("click", () => downloadText(
      state.report.exports.characterization_cgats || "",
      `characterization-${p.name}.txt`,
      "text/plain;charset=utf-8"));
    $(".cal-corr").addEventListener("click", () => downloadText(
      state.report.exports.corrected_cgats || "",
      `characterization-corrected-${p.name}.txt`,
      "text/plain;charset=utf-8"));
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
        const vd = state.applied.verdict;
        const verdictChip = vd ? `
          <span class="badge ${vd.verdict === "improved" ? "badge-green" : vd.verdict === "worsened" ? "badge-red" : "badge-orange"}">较上轮 ${esc(vd.verdict_label)}</span>` : "";
        $("#cal-apply-result").innerHTML = `
          <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap">
            <div class="badge badge-green">${icon("check")}已写入补偿模型（n=${state.applied.n}）</div>
            <span class="chip chip-mono">已记录为第 ${state.applied.round_no} 轮校准</span>
            ${verdictChip}
          </div>
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
async function openCompareModal(view, p) {
  let others = [];
  try {
    others = (await apiGet("/api/processes?include_archived=1")).filter((x) => x.id !== p.id);
  } catch (_) { /* 列表失败也可手动刷新重试 */ }

  const { modal, close } = openModal(`
    <div class="card-title" style="display:flex; align-items:center; gap:8px">
      ${icon("compare")}色域对比 · <span style="color:var(--label-2)">${esc(p.name)}</span>
    </div>
    <div id="cmp-setup" style="margin-top:12px">
      ${others.length ? `
        <label class="page-sub" style="margin:0">对比工艺</label>
        <select id="cmp-other" style="width:100%; margin-top:8px; padding:9px 12px; border:1px solid var(--separator); border-radius:var(--radius-btn); background:var(--fill); color:var(--label); font-size:14px">
          ${others.map((x) => `<option value="${x.id}">${esc(x.name)}（${x.icc_meta.device_space}${isArchived(x) ? " · 已归档" : ""}）</option>`).join("")}
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

function archiveBanner(p) {
  return `
    <section class="archive-banner" aria-label="归档工艺只读信息">
      <div class="archive-banner-icon">${icon("stack")}</div>
      <div class="archive-banner-body">
        <div class="archive-banner-title">已归档 · 只读模式</div>
        <div class="archive-banner-meta">
          <span><b>归档时间</b>${esc(formatArchivedWhen(p.archived_at))}</span>
          <span><b>原因</b>${esc(formatArchiveReason(p.archive_reason) || "未填写")}</span>
          <span><b>操作人</b>${esc(p.archived_by || "未记录")}</span>
        </div>
        <p class="swatch-meta" style="margin:7px 0 0">读取、下载 ICC、护照详情/版本历史与色域对比保持可用；所有写入、校准、签样和护照刷新操作已停用。</p>
      </div>
      <div class="archive-banner-actions">
        <button class="btn btn-secondary btn-sm" id="btn-restore-process">${icon("refresh")}恢复</button>
        <button class="btn btn-danger btn-sm" id="btn-permanent-delete">${icon("trash")}永久删除</button>
      </div>
    </section>`;
}

function openArchiveModal(p, onArchived) {
  const m = openModal(`
    <h3 class="card-title">${icon("stack")}归档工艺 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <div class="issue issue-info" style="margin-top:10px">${icon("info")}<span>归档优先保留证据。归档后工艺转为只读，可随时恢复；不会自动删除 ICC、配方、补偿、校准或护照。</span></div>
    <div class="field" style="margin-top:14px">
      <label>归档原因</label>
      <textarea class="input" id="archive-reason" rows="3" maxlength="120" placeholder="如：设备退役、纸张停产、样张不再投产"></textarea>
    </div>
    <div class="field" style="margin-top:10px">
      <label>操作人 *</label>
      <input class="input" id="archive-actor" maxlength="40" placeholder="姓名">
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn btn-danger" id="archive-confirm">${icon("stack")}确认归档</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#archive-confirm").addEventListener("click", async (event) => {
    const actor = m.modal.querySelector("#archive-actor").value.trim();
    const reason = m.modal.querySelector("#archive-reason").value.trim();
    if (!actor) return toast("请填写归档操作人", "warn");
    const btn = event.currentTarget;
    btn.disabled = true;
    try {
      await apiPost(`/api/processes/${encodeURIComponent(p.id)}/archive`, { reason, actor });
      toast("工艺已归档，可在列表中开启“显示已归档”查看", "ok");
      m.close();
      if (onArchived) onArchived();
    } catch (e) {
      btn.disabled = false;
      toast(e.message, "err");
    }
  });
}

function dependencyValue(dep, keys) {
  for (const key of keys) {
    if (dep && dep[key] !== undefined && dep[key] !== null) return dep[key];
  }
  return null;
}

function dependencyCount(dep, keys, preferred = []) {
  const value = dependencyValue(dep, keys);
  if (value == null) return 0;
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object") {
    for (const key of preferred) {
      if (typeof value[key] === "number") return value[key];
    }
    for (const key of ["count", "total", "rounds", "versions", "n"]) {
      if (typeof value[key] === "number") return value[key];
    }
    const numbers = Object.values(value).filter((v) => typeof v === "number");
    if (numbers.length) return Math.max(...numbers);
  }
  return 0;
}

function dependencySingleCount(dep, keys) {
  const value = dependencyValue(dep, keys);
  if (value == null) return 0;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") return value > 0 ? 1 : 0;
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object") {
    if (typeof value.exists === "boolean") return value.exists ? 1 : 0;
    if (typeof value.present === "boolean") return value.present ? 1 : 0;
    if (typeof value.n === "number" && value.n <= 0) return 0;
    return 1;
  }
  return 0;
}

function openPermanentDeleteModal(p) {
  const m = openModal(`
    <h3 class="card-title">${icon("warn")}永久删除 · <span style="color:var(--label-2)">${esc(p.name)}</span></h3>
    <p class="swatch-meta" style="margin-top:4px">正在读取关联数据，确认前不会执行任何删除。</p>
    <div id="dependency-body" style="margin-top:14px"><div class="spinner"></div></div>
  `);
  const body = m.modal.querySelector("#dependency-body");

  (async () => {
    let deps;
    try {
      deps = await apiGet(`/api/processes/${encodeURIComponent(p.id)}/dependencies`);
    } catch (e) {
      body.innerHTML = `<div class="issue issue-error">${icon("xmark")}<span>关联数据读取失败：${esc(e.message)}</span></div>
        <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
          <button class="btn btn-secondary" data-close>关闭</button>
        </div>`;
      body.querySelector("[data-close]").addEventListener("click", m.close);
      return;
    }
    if (!m.modal.isConnected) return;

    const counts = [
      ["ICC 文件", deps?.icc === false || deps?.has_icc === false ? 0 : 1],
      ["配方", dependencyCount(deps, ["recipes"])],
      ["补偿模型", dependencySingleCount(deps, ["compensation"])],
      ["校准轮次", dependencyCount(deps, ["calibrations", "calibration"], ["rounds"])],
      ["护照版本", dependencyCount(deps, ["passports"], ["versions"])],
      ["测量会话", dependencyCount(deps, ["measurement_sessions", "measurements"], ["total"])],
      ["主观评价", dependencyCount(deps, ["evaluations"], ["total"])],
      ["签样", dependencyCount(deps, ["signoffs"], ["total"])],
    ];
    const blockersRaw = deps?.blockers;
    const blockers = Array.isArray(blockersRaw)
      ? blockersRaw.map((b) => typeof b === "string" ? b : b?.message || b?.reason || JSON.stringify(b)).filter(Boolean)
      : blockersRaw ? [String(blockersRaw)] : [];
    const canDelete = deps?.can_permanent_delete !== false && blockers.length === 0;
    body.innerHTML = `
      <div class="issue issue-error">${icon("warn")}<span>永久删除不可撤销，仅适用于已归档工艺。</span></div>
      <div class="dependency-grid">
        ${counts.map(([label, count]) => `<div class="dependency-item">
          <b>${count}</b><span>${esc(label)}</span>
        </div>`).join("")}
      </div>
      <div class="permanent-delete-notes">
        <div><b>会永久删除</b><span>工艺、ICC 文件、配方、补偿模型、校准轮次与护照版本。</span></div>
        <div><b>会解绑保留</b><span>测量会话与主观评价解除工艺归属，但仍保留作审计证据。</span></div>
        <div><b>会标记孤儿</b><span>签样记录不删除，标记为孤儿并保留标题、状态与历史。</span></div>
      </div>
      ${blockers.length ? `<div class="issue-list" style="margin-top:12px">${blockers.map((b) =>
        `<div class="issue issue-warn">${icon("warn")}<span>${esc(b)}</span></div>`).join("")}</div>` : ""}
      <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:18px">
        <button class="btn btn-ghost" data-cancel>取消</button>
        <button class="btn btn-danger" id="dependency-delete"
          ${canDelete ? "" : `disabled aria-disabled="true" title="存在阻断项，当前不能永久删除"`}>${icon("trash")}永久删除</button>
      </div>`;
    body.querySelector("[data-cancel]").addEventListener("click", m.close);
    body.querySelector("#dependency-delete").addEventListener("click", async (event) => {
      const btn = event.currentTarget;
      const ok = await confirmDanger({
        title: "永久删除该工艺？",
        message: "ICC、配方、补偿、校准与护照将删除；测量/评价解绑保留，签样标记孤儿。此操作不可撤销。",
        confirmText: "永久删除",
      });
      if (!ok) return;
      btn.disabled = true;
      try {
        await apiDelete(
          `/api/processes/${encodeURIComponent(p.id)}/permanent?confirm=${encodeURIComponent(p.id)}`
        );
        toast("工艺已永久删除，测量/评价与签样审计证据已保留", "ok");
        m.close();
        updateNavCount();
        location.hash = "#/processes";
      } catch (e) {
        btn.disabled = false;
        toast(e.message, "err");
      }
    });
  })();
}

export async function renderProcessDetail(view, params) {
  const isActive = params.isActive || (() => true);
  const signal = params.signal;
  let activeGamutView = null;
  const disposeDetail = () => {
    if (activeGamutView && typeof activeGamutView.destroy === "function") {
      try { activeGamutView.destroy(); } catch (_) {}
      activeGamutView = null;
    }
  };
  params.onCleanup?.(disposeDetail);

  view.innerHTML = `<div class="spinner"></div>`;
  let p;
  try {
    p = await apiGet(`/api/processes/${encodeURIComponent(params.id)}`, { signal });
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (!isActive()) return;
    view.innerHTML = errorState(e.message, { retry: false });
    return;
  }
  if (!isActive()) return;

  const archived = isArchived(p);
  const gamutPromise = apiGet(`/api/processes/${encodeURIComponent(p.id)}/gamut?levels=7`, { signal });
  const recipesPromise = apiGet(`/api/recipes?process_id=${encodeURIComponent(p.id)}`, { signal });

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
        <button class="btn btn-sm" id="btn-use"
          ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能用此工艺预测"` : ""}>${icon("wand")}用此工艺预测</button>
        <button class="btn btn-secondary btn-sm" id="btn-edit"
          ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能编辑"` : ""}>${icon("pencil")}编辑</button>
        ${archived ? "" : `<button class="btn btn-danger btn-sm" id="btn-archive"
          title="归档后工艺只读，可随时恢复">${icon("stack")}归档</button>`}
      </div>
    </div>

    ${archived ? archiveBanner(p) : ""}

    <div class="stat-grid">
      <div class="card stat"><div class="stat-val" id="st-volume"><span class="spinner"></span></div><div class="stat-label">色域体积（Lab 包围盒）</div></div>
      <div class="card stat"><div class="stat-val" id="st-lrange"><span class="spinner"></span></div><div class="stat-label">L* 明度范围</div></div>
      <div class="card stat"><div class="stat-val" id="st-cov"><span class="spinner"></span></div><div class="stat-label">sRGB 覆盖率</div></div>
      <div class="card stat"><div class="stat-val" id="st-pass"><span class="spinner"></span></div><div class="stat-label">配方达标率（近 20 条）</div></div>
    </div>

    ${p.compensation && p.compensation.n > 0 ? compensationCard(p, archived) : ""}
    <div id="passport-slot">${skeletonCards(1)}</div>
    ${p.icc_meta.device_space === "CMYK" ? `<div id="cal-history-slot">${skeletonCards(1)}</div>` : ""}
    <div id="signoff-slot">${skeletonCards(1)}</div>

    <div class="card gamut-card">
      <div class="gamut-card-head">
        <div class="card-title">${icon("target")}工艺色域</div>
        <div class="gamut-actions">
          ${p.icc_meta.device_space === "CMYK"
            ? `<button class="btn btn-secondary btn-sm" id="btn-calibration"
                ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能打印校准"` : ""}>${icon("printer")}打印校准</button>`
            : ""}
          <button class="btn btn-secondary btn-sm" id="btn-testform"
            ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能生成验证样张"` : ""}>${icon("doc")}生成验证样张</button>
          <button class="btn btn-secondary btn-sm" id="btn-compare">${icon("compare")}色域对比</button>
        </div>
      </div>
      <div id="gamut-view"></div>
    </div>

    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:14px; align-items:start">
      <div class="card">
        <div class="card-title">${icon("stack")}ICC 特性文件</div>
        <dl class="kv" style="margin-top:10px">
          <dt>设备色彩空间</dt><dd>${esc(p.icc_meta.device_space)}（${p.icc_meta.channels} 通道）</dd>
          <dt>测量条件</dt><dd>${esc(p.measurement_condition || "未登记")}</dd>
          <dt>ICC 指纹</dt><dd><span class="chip chip-mono">${esc((p.icc_sha256 || "").slice(0, 8))}</span></dd>
          <dt>文件名</dt><dd>${esc(p.icc_original_name)}</dd>
          <dt>大小</dt><dd>${(p.icc_size / 1024).toFixed(1)} KB</dd>
          <dt>描述</dt><dd>${esc(p.icc_meta.description || "-")}</dd>
          <dt>版权</dt><dd>${esc(p.icc_meta.copyright || "-")}</dd>
          ${p.paper ? `<dt>纸张</dt><dd>${esc(p.paper)}</dd>` : ""}
          ${p.ink ? `<dt>墨水</dt><dd>${esc(p.ink)}</dd>` : ""}
          ${p.notes ? `<dt>备注</dt><dd>${esc(p.notes)}</dd>` : ""}
          <dt>创建时间</dt><dd>${esc(p.created_at)}</dd>
        </dl>
        <div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap">
          <a class="btn btn-secondary btn-sm" href="/api/processes/${encodeURIComponent(p.id)}/icc" download="${esc(p.icc_original_name)}">${icon("download")}下载 ICC</a>
          <button class="btn btn-secondary btn-sm" id="btn-replace"
            ${archived ? `disabled aria-disabled="true" title="工艺已归档，恢复后才能替换 ICC"` : ""}>${icon("swap")}替换 ICC</button>
        </div>
      </div>

      <div>
        <div class="section-title" style="margin-top:0">该工艺的配方历史</div>
        <div class="card" id="recipe-history"><div class="spinner"></div></div>
      </div>
    </div>
  `;

  const reload = () => {
    disposeDetail();
    return renderProcessDetail(view, params);
  };
  renderPassportSlot(view, p, isActive, signal);

  const compResetBtn = view.querySelector("#btn-comp-reset");
  if (compResetBtn && !archived) {
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
  if (!archived) {
    view.querySelector("#btn-use").addEventListener("click", () => (location.hash = `#/recipes?process=${encodeURIComponent(p.id)}`));
    view.querySelector("#btn-edit").addEventListener("click", () =>
      openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
    );
    view.querySelector("#btn-replace").addEventListener("click", () =>
      openProcessEditor(view, { mode: "edit", process: p, onDone: reload })
    );
    view.querySelector("#btn-archive").addEventListener("click", () =>
      openArchiveModal(p, () => {
        updateNavCount();
        location.hash = "#/processes";
      })
    );
  }
  view.querySelector("#btn-compare").addEventListener("click", () => openCompareModal(view, p));
  if (!archived) view.querySelector("#btn-testform").addEventListener("click", () => openTestformModal(view, p));
  const calBtn = view.querySelector("#btn-calibration");
  if (calBtn && !archived) calBtn.addEventListener("click", () => openCalibrationModal(view, p));

  // V0.8：校准轮次历史（异步填充；失败静默——无轮次不显示卡片）
  const calSlot = view.querySelector("#cal-history-slot");
  if (calSlot) {
    (async () => {
      try {
        const hist = await apiGet(`/api/qc/calibration/history?process_id=${encodeURIComponent(p.id)}`, { signal });
        if (!isActive() || !calSlot.isConnected) return;
        if (!hist.rounds.length) { calSlot.innerHTML = ""; return; }
        calSlot.innerHTML = calibrationHistoryCard(hist, archived);
        if (!archived) {
          calSlot.querySelector("[data-cal-open]")?.addEventListener("click", () =>
            openCalibrationModal(view, { ...p, compensation: undefined }));
        }
      } catch (e) {
        if (e && e.name === "AbortError") return;
        if (isActive() && calSlot.isConnected) calSlot.innerHTML = "";
      }
    })();
  }

  // V0.9：签样卡（状态机 + 放行门禁；ICC 变更后门禁自动失效）
  const sgSlot = view.querySelector("#signoff-slot");
  const loadSignoffSlot = async () => {
    if (!sgSlot) return;
    try {
      const [rows, gate] = await Promise.all([
        apiGet(`/api/signoffs?process_id=${encodeURIComponent(p.id)}`, { signal }),
        apiGet(`/api/signoffs/gate?process_id=${encodeURIComponent(p.id)}`, { signal }),
      ]);
      if (!isActive() || !sgSlot.isConnected) return;
      sgSlot.innerHTML = signoffCard(rows, gate, archived);
      const card = sgSlot.querySelector(".card");
      card.querySelectorAll("[data-sg]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const action = btn.dataset.sg;
          if (action === "create") return openSignoffCreate(p, reload);
          if (action === "manifest") return downloadSignoffManifest(p);
          if (action === "delete") {
            const ok = await confirmDanger({
              title: "删除签样草稿？",
              message: "草稿及其证据关联将被移除，此操作不可撤销。",
              confirmText: "删除",
            });
            if (!ok) return;
            try {
              await apiDelete(`/api/signoffs/${encodeURIComponent(rows[0].id)}`);
              toast("签样草稿已删除", "ok");
              reload();
            } catch (e) {
              toast(e.message, "err");
            }
            return;
          }
          return openSignoffAction(action, rows[0], reload);
        });
      });
    } catch (e) {
      if (e && e.name === "AbortError") return;
      if (!isActive() || !sgSlot.isConnected) return;
      sgSlot.innerHTML = errorState(`签样门禁加载失败：${e.message}`);
      sgSlot.querySelector(".btn-retry")?.addEventListener("click", loadSignoffSlot);
    }
  };
  if (sgSlot) loadSignoffSlot();

  const restoreBtn = view.querySelector("#btn-restore-process");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", async () => {
      restoreBtn.disabled = true;
      try {
        await apiPost(`/api/processes/${encodeURIComponent(p.id)}/restore`, {});
        toast("工艺已恢复为可编辑状态", "ok");
        updateNavCount();
        reload();
      } catch (e) {
        restoreBtn.disabled = false;
        toast(e.message, "err");
      }
    });
  }
  view.querySelector("#btn-permanent-delete")?.addEventListener("click", () => openPermanentDeleteModal(p));

  // 色域主区（2D/3D 组件）+ 概览指标
  try {
    const [gamut, recipes] = await Promise.all([gamutPromise, recipesPromise]);
    if (!isActive()) return; // 用户已切走
    view.querySelector("#st-volume").textContent = formatVolume(gamut.volume);
    view.querySelector("#st-lrange").textContent = `${gamut.L_range[0]} – ${gamut.L_range[1]}`;
    view.querySelector("#st-cov").textContent = `${Math.round(gamut.coverage_srgb * 100)}%`;
    const recent = recipes.slice(0, 20);
    view.querySelector("#st-pass").innerHTML = recent.length
      ? passRateHtml(recent.filter((r) => r.converged).length / recent.length)
      : `<span class="pass-none">—</span>`;

    disposeDetail();
    const gamutHolder = view.querySelector("#gamut-view");
    if (gamutHolder) {
      activeGamutView = createGamutView(gamutHolder, {
        getData: () => gamutPromise,
        levels: 7,
      });
    }

    renderHistory(view, recipes);
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (!isActive()) return;
    const holder = view.querySelector("#gamut-view");
    if (holder) holder.innerHTML = errorState(`色域数据加载失败：${e.message}`);
  }
}

function renderHistory(view, recipes) {
  const box = view.querySelector("#recipe-history");
  if (!box) return;
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
          <div class="row-sub">${Object.entries(r.recipe).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ")} · 目标 Lab(${r.target_lab.map((x) => Math.round(x)).join(",")})</div>
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
