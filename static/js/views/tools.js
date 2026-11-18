/* 工具箱视图：CMYK→LAB 正查 · ΔE 对比 · 色域对比 · 色彩转换器 · 显示器档案 */

import { apiGet, apiPost, apiDelete, apiPostRaw, downloadDataUri } from "../api.js";
import { icon } from "../icons.js";
import {
  toast, swatch, drawCie, labToHex, fetchSpectrum, emptyState, observeRedraw,
  openModal, confirmDanger,
} from "../components.js";
import { SRGB_XY } from "../gamut-view.js";

let processes = [];
let displays = [];

const TOOLS = [
  ["forward", "CMYK→LAB 正查", "给定墨量配方预测颜色"],
  ["deltae", "ΔE 对比", "两组 Lab 颜色色差检测"],
  ["gamut", "色域对比", "CIE 1931 色度图"],
  ["convert", "色彩转换器", "常见色彩空间互转"],
  ["display", "显示器", "校准档案与软打样门禁"],
];

export async function renderToolsView(view) {
  try {
    const [procs, disps] = await Promise.all([
      apiGet("/api/processes"),
      apiGet("/api/displays").catch(() => []),
    ]);
    processes = procs;
    displays = disps;
  } catch (_) {
    processes = [];
    displays = [];
  }

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h1 class="large-title">工具箱</h1>
        <p class="page-sub">专业色彩工具：正查、色差、色域与转换</p>
      </div>
    </div>
    <div class="seg" id="tool-seg" style="margin-bottom:18px">
      ${TOOLS.map(([k, t]) => `<button data-tool="${k}" class="${k === "forward" ? "active" : ""}">${t}</button>`).join("")}
    </div>
    ${TOOLS.map(([k, t, d], i) => `
      <div class="tool-pane ${k === "forward" ? "active" : ""}" data-pane="${k}">
        <div class="card">
          <div class="card-title">${t}</div>
          <div class="card-desc">${d}</div>
          <div id="tool-${k}"></div>
        </div>
      </div>
    `).join("")}
  `;

  view.querySelectorAll("#tool-seg button").forEach((btn) => {
    btn.addEventListener("click", () => {
      view.querySelectorAll("#tool-seg button").forEach((b) => b.classList.toggle("active", b === btn));
      view.querySelectorAll(".tool-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === btn.dataset.tool));
    });
  });

  buildForward(view.querySelector("#tool-forward"));
  buildDeltaE(view.querySelector("#tool-deltae"));
  buildGamut(view.querySelector("#tool-gamut"));
  buildConvert(view.querySelector("#tool-convert"));
  buildDisplay(view.querySelector("#tool-display"));
}

/* ---------- CMYK→LAB 正查 ---------- */
function buildForward(box) {
  if (!processes.length) {
    box.innerHTML = emptyState({ iconName: "stack", title: "需要先创建工艺", desc: "正查基于工艺 ICC，先到工艺库创建。" });
    return;
  }
  box.innerHTML = `
    <div class="field-row">
      <div class="field" style="max-width:340px">
        <label>工艺</label>
        <select class="input" id="fq-process">
          ${processes.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.icc_meta.device_space}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="field-row" id="fq-channels" style="margin-top:10px"></div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="fq-go">${icon("wand")}查询</button>
      <div id="fq-result" style="display:flex; align-items:center; gap:10px"></div>
    </div>
  `;

  const renderChannels = () => {
    const p = processes.find((x) => x.id === box.querySelector("#fq-process").value);
    const names = { CMYK: ["C", "M", "Y", "K"], RGB: ["R", "G", "B"], GRAY: ["K"] }[p.icc_meta.device_space] || ["C", "M", "Y", "K"];
    box.querySelector("#fq-channels").innerHTML = names
      .map((n) => `<div class="field" style="max-width:110px"><label>${n} %</label><input class="input fq-v" data-ch="${n}" type="number" min="0" max="100" value="0"></div>`)
      .join("");
  };
  renderChannels();
  box.querySelector("#fq-process").addEventListener("change", renderChannels);

  box.querySelector("#fq-go").addEventListener("click", async () => {
    const values = [...box.querySelectorAll(".fq-v")].map((i) => parseFloat(i.value) || 0);
    try {
      const r = await apiPost("/api/recipes/forward", {
        process_id: box.querySelector("#fq-process").value,
        values,
      });
      box.querySelector("#fq-result").innerHTML = `
        <div class="swatch" style="background:${r.hex}"></div>
        <div class="swatch-meta">Lab(${r.lab.join(", ")})<br>${r.hex}</div>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* ---------- ΔE 对比 ---------- */
function buildDeltaE(box) {
  box.innerHTML = `
    <div class="field-row">
      <div class="field"><label>颜色 1 · L*</label><input class="input de1" id="de-l1" type="number" value="50"></div>
      <div class="field"><label>a*</label><input class="input de1" id="de-a1" type="number" value="20"></div>
      <div class="field"><label>b*</label><input class="input de1" id="de-b1" type="number" value="5"></div>
      <div class="divider-v" style="align-self:center; height:38px"></div>
      <div class="field"><label>颜色 2 · L*</label><input class="input de2" id="de-l2" type="number" value="52"></div>
      <div class="field"><label>a*</label><input class="input de2" id="de-a2" type="number" value="18"></div>
      <div class="field"><label>b*</label><input class="input de2" id="de-b2" type="number" value="7"></div>
      <div class="field"><label>容差</label><input class="input" id="de-tol" type="number" value="2" step="0.1" style="max-width:90px"></div>
    </div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="de-go">${icon("target")}计算 ΔE</button>
      <div id="de-result" style="display:flex;align-items:center;gap:10px"></div>
    </div>
  `;
  box.querySelector("#de-go").addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    try {
      const r = await apiPost("/api/delta-e/lab", {
        lab1: [g("de-l1"), g("de-a1"), g("de-b1")],
        lab2: [g("de-l2"), g("de-a2"), g("de-b2")],
        tolerance: g("de-tol") || 2,
      });
      box.querySelector("#de-result").innerHTML = `
        <span class="badge ${r.passed ? "badge-green" : "badge-red"}">ΔE ${r.delta_e} ${r.passed ? "· 通过" : "· 超差"}</span>
        <span class="swatch" style="background:${labToHex(r.lab1)}"></span>
        <span class="swatch" style="background:${labToHex(r.lab2)}"></span>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* ---------- 色域对比 ---------- */
async function buildGamut(box) {
  box.innerHTML = `
    <div class="field-row" style="margin-bottom:12px">
      <div class="field" style="max-width:340px">
        <label>叠加工艺点集</label>
        <select class="input" id="gm-process">
          <option value="">不叠加</option>
          ${processes.map((p) => `<option value="${p.id}">${esc(p.name)} · ${p.icc_meta.device_space}</option>`).join("")}
        </select>
      </div>
    </div>
    <canvas class="cie-canvas" id="gm-cie"></canvas>
    <div class="cie-legend">
      <span><i class="dot" style="background:linear-gradient(90deg,#4400ff,#00ff00,#ff0000)"></i>光谱轨迹</span>
      <span><i class="dot legend-srgb"></i>sRGB</span>
      <span><i class="dot" style="background:var(--teal)"></i>Display P3</span>
      <span><i class="dot" style="background:var(--yellow)"></i>Adobe RGB</span>
      <span><i class="dot legend-proc"></i>工艺凸包 · 采样点按 L* 着色</span>
    </div>
  `;

  const SPECTRA = [
    { color: "var(--accent)", spaces: ["srgb"] },
    { color: "var(--teal)", spaces: ["dci-p3"] },
    { color: "var(--yellow)", spaces: ["adobe-rgb"] },
  ];
  const canvas = box.querySelector("#gm-cie");
  let spectrum = null;
  try {
    spectrum = await fetchSpectrum();
  } catch (_) { /* 光谱数据失败仍可画色域 */ }
  const draw = async () => {
    const series = [];
    // sRGB 参考色域以嵌套底图为准（与工艺库详情页同源）
    series.push({ points: SRGB_XY, color: "var(--accent)", fill: true, fillAlpha: 0.13 });
    try {
      const data = await apiGet("/api/visualization/gamut?spaces=dci-p3,adobe-rgb");
      for (const s of SPECTRA) {
        if (s.spaces[0] === "srgb") continue;
        const poly = s.spaces.flatMap((sp) => data.gamut_polygons[sp] || []);
        if (poly.length) series.push({ points: poly, color: s.color, fill: true });
      }
    } catch (_) { /* 忽略 */ }
    const pid = box.querySelector("#gm-process").value;
    if (pid) {
      try {
        const g = await apiGet(`/api/processes/${pid}/gamut?levels=7`);
        series.push({
          points: g.xy_points,
          labPoints: g.lab_points,
          hull: g.hull,
          color: "var(--indigo)",
          pointSize: 1.8,
        });
      } catch (e) {
        toast(`工艺色域加载失败：${e.message}`, "err");
      }
    }
    drawCie(canvas, series, { spectrum });
    if (!canvas.dataset.observed) {
      canvas.dataset.observed = "1";
      observeRedraw(canvas, draw);
    }
  };
  canvas.style.maxWidth = "440px";
  draw();
  box.querySelector("#gm-process").addEventListener("change", draw);
}

/* ---------- 色彩转换器 ---------- */
function buildConvert(box) {
  box.innerHTML = `
    <div class="field-row">
      <div class="field"><label>R</label><input class="input cv" id="cv-r" type="number" min="0" max="255" value="180"></div>
      <div class="field"><label>G</label><input class="input cv" id="cv-g" type="number" min="0" max="255" value="130"></div>
      <div class="field"><label>B</label><input class="input cv" id="cv-b" type="number" min="0" max="255" value="85"></div>
      <div class="field"><label>源空间</label>
        <select class="input" id="cv-from">
          <option value="srgb">sRGB</option><option value="adobe-rgb">Adobe RGB</option>
          <option value="dci-p3">Display P3</option><option value="rec2020">Rec.2020</option>
          <option value="prophoto">ProPhoto</option>
        </select>
      </div>
      <div class="field"><label>目标空间</label>
        <select class="input" id="cv-to">
          <option value="lab">CIELAB</option><option value="xyz">CIEXYZ</option>
          <option value="srgb">sRGB</option><option value="dci-p3">Display P3</option>
          <option value="adobe-rgb">Adobe RGB</option>
        </select>
      </div>
    </div>
    <div style="margin-top:14px; display:flex; align-items:center; gap:12px">
      <button class="btn" id="cv-go">${icon("swap")}转换</button>
      <div id="cv-result" style="display:flex;align-items:center;gap:10px"></div>
    </div>
  `;
  box.querySelector("#cv-go").addEventListener("click", async () => {
    const g = (id) => parseFloat(box.querySelector("#" + id).value);
    try {
      const r = await apiPost("/api/convert", {
        rgb: [g("cv-r"), g("cv-g"), g("cv-b")],
        from_space: box.querySelector("#cv-from").value,
        to_space: box.querySelector("#cv-to").value,
      });
      const isLab = box.querySelector("#cv-to").value === "lab";
      box.querySelector("#cv-result").innerHTML = `
        ${isLab ? `<div class="swatch" style="background:${labToHex(r.result)}"></div>` : ""}
        <span class="swatch-meta">${box.querySelector("#cv-to").value.toUpperCase()}(${r.result.map((v) => Math.round(v * 1000) / 1000).join(", ")})</span>`;
    } catch (e) {
      toast(e.message, "err");
    }
  });
}

/* ---------- 显示器档案与软打样门禁（V0.9 P0-4） ---------- */
const DISP_STATUS_BADGE = {
  ok: "badge badge-green",
  expiring: "badge badge-orange",
  expired: "badge badge-red",
  uncalibrated: "badge badge-gray",
  none: "badge badge-gray",
};

function todayLocal() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function dispStatusBadge(st) {
  return `<span class="${DISP_STATUS_BADGE[st?.status] || "badge badge-gray"}">${esc(st?.status_label || "未校准")}</span>`;
}

function buildDisplay(box) {
  if (!box) return;
  const rows = displays.map((d) => {
    const st = d.status || {};
    const meas = d.measurement;
    const measLine = meas
      ? `<div class="row-sub">测量：${meas.patches} 块 · 白点 (${(meas.white_xy || []).map((v) => Number(v).toFixed(4)).join(", ") || "—"}) · 对比度 ${meas.contrast_ratio ?? "—"}:1${meas.luminance_cd_m2 != null ? ` · ${meas.luminance_cd_m2} cd/m²` : ""} · ${esc(meas.filename)}</div>`
      : "";
    return `<div class="group-row" data-disp-id="${d.id}">
      <div class="grow">
        <div class="row-title">${esc(d.name)}${d.is_active ? '<span class="badge badge-blue" style="margin-left:6px">当前</span>' : ""} ${dispStatusBadge(st)}</div>
        <div class="row-sub">${esc(d.model || "型号未填")} · 目标 ${esc(d.targets.white_point)} / ${d.targets.luminance_cd_m2} cd/m² / ${d.targets.contrast_ratio}:1${st.expires_at ? ` · 到期 ${esc(st.expires_at)}` : ""}</div>
        <div class="row-sub">${esc(st.message || "")}</div>
        ${measLine}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${d.is_active ? "" : `<button class="btn btn-secondary btn-sm" data-disp-act="activate">设为当前</button>`}
        <button class="btn btn-secondary btn-sm" data-disp-act="calibrate">校准确认</button>
        <button class="btn btn-secondary btn-sm" data-disp-act="measure">导入测量</button>
        ${meas ? `<button class="btn btn-ghost btn-sm" data-disp-act="export-measure">导出测量</button>` : ""}
        <button class="btn btn-ghost btn-sm" data-disp-act="report">报告</button>
        <button class="btn btn-ghost btn-sm" data-disp-act="delete" style="color:var(--red)">删除</button>
      </div>
    </div>`;
  }).join("");
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <div class="card-desc" style="margin:0">软打样门禁：校准有效（到期前 30 天提醒）才标记为可信预览；系统不生成显示器 ICC。</div>
      <button class="btn btn-sm" id="disp-add">${icon("plus")}新增显示器</button>
    </div>
    ${rows || emptyState({
      iconName: "eye", title: "尚未登记显示器",
      desc: "登记显示器与校准确认后，软打样预览才会标记为可信；未登记时预览仅参考。",
    })}`;
  box.querySelector("#disp-add")?.addEventListener("click", () => openDisplayCreate(box));
  box.querySelectorAll("[data-disp-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = btn.closest("[data-disp-id]");
      const id = row?.dataset.dispId;
      const act = btn.dataset.dispAct;
      const disp = displays.find((d) => d.id === id);
      if (act === "activate") {
        try {
          await apiPost(`/api/displays/${id}/activate`, {});
          toast("已切换当前显示器", "ok");
          refreshDisplayPane(box);
        } catch (e) { toast(e.message, "err"); }
      } else if (act === "calibrate") {
        openDisplayCalibration(disp, box);
      } else if (act === "measure") {
        openDisplayMeasurement(disp, box);
      } else if (act === "export-measure") {
        try {
          const resp = await fetch(`/api/displays/${id}/measurements/latest`);
          if (!resp.ok) {
            let detail = "下载失败";
            try { detail = (await resp.json()).detail || detail; } catch (_) { /* 保留默认 */ }
            throw new Error(detail);
          }
          const text = await resp.text();
          downloadDataUri(
            "data:text/plain;charset=utf-8," + encodeURIComponent(text),
            disp.measurement?.filename || "display-measurement.txt",
          );
        } catch (e) { toast(e.message, "err"); }
      } else if (act === "report") {
        try {
          const report = await apiGet(`/api/displays/${id}/report`);
          downloadDataUri(
            "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(report, null, 2)),
            `显示器校准-${report.display.name}.json`,
          );
        } catch (e) { toast(e.message, "err"); }
      } else if (act === "delete") {
        const ok = await confirmDanger({
          title: `删除显示器「${disp?.name || ""}」？`,
          message: "档案与校准历史将被移除，软打样门禁退回「未登记」。",
          confirmText: "删除",
        });
        if (!ok) return;
        try {
          await apiDelete(`/api/displays/${id}`);
          toast("显示器档案已删除", "ok");
          refreshDisplayPane(box);
        } catch (e) { toast(e.message, "err"); }
      }
    });
  });
}

async function refreshDisplayPane(box) {
  displays = await apiGet("/api/displays").catch(() => []);
  buildDisplay(box);
}

function openDisplayCreate(box) {
  const m = openModal(`
    <h3 class="card-title">${icon("eye")}新增显示器</h3>
    <div class="field-row">
      <div class="field"><label>名称 *</label><input class="input" id="dp-name" maxlength="80" placeholder="如 Eizo CG2700X（主）"></div>
      <div class="field"><label>型号</label><input class="input" id="dp-model" maxlength="80"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>目标白点</label><select class="input" id="dp-wp"><option>D50</option><option>D55</option><option>D65</option></select></div>
      <div class="field"><label>目标亮度 cd/m²</label><input class="input" id="dp-lum" type="number" value="120" min="40" max="400"></div>
      <div class="field"><label>目标对比度</label><input class="input" id="dp-contrast" type="number" value="1000" min="100"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准日期</label><input class="input" id="dp-cal" type="date" value="${todayLocal()}"></div>
      <div class="field"><label>有效期（月）</label><input class="input" id="dp-months" type="number" value="6" min="1" max="60"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准器</label><input class="input" id="dp-calibrator" maxlength="80" placeholder="i1Display Pro"></div>
      <div class="field"><label>显示器 ICC 名称</label><input class="input" id="dp-profile" maxlength="120" placeholder="Eizo_D50.icc"></div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dp-save">${icon("plus")}创建</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#dp-save").addEventListener("click", async () => {
    const name = m.modal.querySelector("#dp-name").value.trim();
    if (!name) return toast("请填写显示器名称", "warn");
    const whitePoint = m.modal.querySelector("#dp-wp").value;
    const calDate = m.modal.querySelector("#dp-cal").value;
    const calibration = calDate ? {
      calibrated_at: calDate,
      valid_months: Number(m.modal.querySelector("#dp-months").value) || 6,
      calibrator: m.modal.querySelector("#dp-calibrator").value.trim(),
      white_point: whitePoint,
      profile_name: m.modal.querySelector("#dp-profile").value.trim(),
    } : null;
    try {
      await apiPost("/api/displays", {
        name,
        model: m.modal.querySelector("#dp-model").value.trim(),
        is_active: true,
        targets: {
          white_point: whitePoint,
          luminance_cd_m2: Number(m.modal.querySelector("#dp-lum").value) || 120,
          contrast_ratio: Number(m.modal.querySelector("#dp-contrast").value) || 1000,
        },
        calibration,
      });
      toast("显示器档案已创建", "ok");
      m.close();
      refreshDisplayPane(box);
    } catch (e) { toast(e.message, "err"); }
  });
}

function openDisplayCalibration(disp, box) {
  if (!disp) return;
  const whitePoint = disp.targets?.white_point || "D50";
  const m = openModal(`
    <h3 class="card-title">${icon("check")}校准确认 · <span style="color:var(--label-2)">${esc(disp.name)}</span></h3>
    <div class="field-row">
      <div class="field"><label>校准日期 *</label><input class="input" id="dc-cal" type="date" value="${todayLocal()}"></div>
      <div class="field"><label>有效期（月）</label><input class="input" id="dc-months" type="number" value="6" min="1" max="60"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>校准器</label><input class="input" id="dc-calibrator" maxlength="80" value="${esc(disp.calibration?.calibrator || "")}"></div>
      <div class="field"><label>软件</label><input class="input" id="dc-software" maxlength="80" value="${esc(disp.calibration?.software || "")}" placeholder="DisplayCAL"></div>
    </div>
    <div class="field-row" style="margin-top:10px">
      <div class="field"><label>实测亮度 cd/m²</label><input class="input" id="dc-lum" type="number" step="0.1" min="0"></div>
      <div class="field"><label>实测对比度</label><input class="input" id="dc-contrast" type="number" step="1" min="0"></div>
    </div>
    <div class="field" style="margin-top:10px"><label>显示器 ICC 名称</label><input class="input" id="dc-profile" maxlength="120" value="${esc(disp.calibration?.profile_name || "")}" placeholder="外部工具生成的显示器 profile"></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dc-save">${icon("check")}保存确认</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#dc-save").addEventListener("click", async () => {
    const calDate = m.modal.querySelector("#dc-cal").value;
    if (!calDate) return toast("请填写校准日期", "warn");
    const lum = m.modal.querySelector("#dc-lum").value;
    const contrast = m.modal.querySelector("#dc-contrast").value;
    try {
      await apiPost(`/api/displays/${disp.id}/calibrations`, {
        calibrated_at: calDate,
        valid_months: Number(m.modal.querySelector("#dc-months").value) || 6,
        calibrator: m.modal.querySelector("#dc-calibrator").value.trim(),
        software: m.modal.querySelector("#dc-software").value.trim(),
        white_point: whitePoint,
        luminance_cd_m2: lum === "" ? null : Number(lum),
        contrast_ratio: contrast === "" ? null : Number(contrast),
        profile_name: m.modal.querySelector("#dc-profile").value.trim(),
      });
      toast("校准确认已保存", "ok");
      m.close();
      refreshDisplayPane(box);
    } catch (e) { toast(e.message, "err"); }
  });
}

function openDisplayMeasurement(disp, box) {
  if (!disp) return;
  const m = openModal(`
    <h3 class="card-title">${icon("upload")}导入屏幕测量 · <span style="color:var(--label-2)">${esc(disp.name)}</span></h3>
    <div class="field"><label>测量文件（CGATS.17 / TSV）*</label><input class="input" id="dm-file" type="file" accept=".txt,.cgats,.ti3,.tsv,.csv"></div>
    <div class="field" style="margin-top:10px"><label>白场绝对亮度 cd/m²（可选）</label><input class="input" id="dm-lum" type="number" step="0.1" min="0" placeholder="如 120；文件头含 LUMINANCE_CD_M2 可留空"></div>
    <div class="issue issue-info" style="margin-top:12px">${icon("info")}<span>需至少 2 个含 XYZ 的色块（白 / 黑）；系统据此推导白点与对比度，并原样留存文件供导出复核。</span></div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px">
      <button class="btn btn-ghost" data-cancel>取消</button>
      <button class="btn" id="dm-save">${icon("upload")}导入</button>
    </div>
  `);
  m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);
  m.modal.querySelector("#dm-save").addEventListener("click", async () => {
    const input = m.modal.querySelector("#dm-file");
    const file = input.files && input.files[0];
    if (!file) return toast("请选择测量文件", "warn");
    const lum = m.modal.querySelector("#dm-lum").value;
    const query = { filename: file.name };
    if (lum !== "") query.white_luminance_cd_m2 = lum;
    try {
      await apiPostRaw(
        `/api/displays/${disp.id}/measurements`,
        query,
        new Uint8Array(await file.arrayBuffer()),
      );
      toast("屏幕测量已导入", "ok");
      m.close();
      refreshDisplayPane(box);
    } catch (e) { toast(e.message, "err"); }
  });
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
