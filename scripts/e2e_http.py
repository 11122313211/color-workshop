"""HTTP 端到端验证：走通 工艺创建→配方预测→实测验证→预检→转换→清理。"""

import base64
import hashlib
import io
import sys
from pathlib import Path

import httpx
from PIL import Image

# 控制台可能为 GBK：check 标签含 −/× 等 Unicode 字符，固定 UTF-8 输出避免编码崩溃
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本地固定后端地址（安全：目标硬编码为本机回环地址，不读取任何外部输入）
client = httpx.Client(base_url="http://127.0.0.1:8765")
WIN = Path(r"C:/Windows/System32/spool/drivers/color")


def check(label, cond, extra=""):
    mark = "[PASS]" if cond else "[FAIL]"
    print(f"{mark} {label} {extra}")
    return bool(cond)


ok = True

# 0. 静态页与 API 健康
r = client.get("/api/health")
ok &= check("健康检查", r.status_code == 200 and r.json()["status"] == "ok", r.text[:80])
ok &= check("版本号 v0.9.0", r.json().get("version") == "0.9.0")
r = client.get("/", follow_redirects=False)
ok &= check("根路径重定向", r.status_code == 307 and r.headers["location"] == "/static/index.html")
r = client.get("/static/index.html")
ok &= check("前端首页", r.status_code == 200 and "色彩工坊" in r.text)
for path in ("/static/css/app.css", "/static/js/main.js", "/static/js/views/recipes.js"):
    r = client.get(path)
    ok &= check(f"静态资源 {path}", r.status_code == 200)

# 1. 创建工艺（raw bytes 上传）
icc = (WIN / "RSWOP.icm").read_bytes()
r = client.post(
    "/api/processes",
    params={
        "name": "E2E-SWOP",
        "paper": "铜版纸",
        "ink": "四色",
        "filename": "RSWOP.icm",
    },
    content=icc,
    headers={"Content-Type": "application/octet-stream"},
)
ok &= check(
    "创建工艺（ICC 上传）",
    r.status_code == 200 and r.json()["icc_meta"]["device_space"] == "CMYK",
    str(r.json().get("icc_meta", {}).get("description", ""))[:40],
)
pid = r.json()["id"]

r = client.get("/api/processes")
ok &= check("工艺列表", r.status_code == 200 and any(p["id"] == pid for p in r.json()))

r = client.get(f"/api/processes/{pid}/gamut?levels=5")
ok &= check("工艺色域采样", r.status_code == 200 and len(r.json()["xy_points"]) == 5**4)

# 2. 批量配方预测
r = client.post(
    "/api/recipes/predict",
    json={
        "process_id": pid,
        "targets": [
            {"label": "专色红", "lab": [50.0, 20.0, 5.0]},
            {"label": "青", "lab": [50.0, -20.0, -25.0]},
        ],
        "tolerance": 2.0,
    },
)
data = r.json()
ok &= check(
    "批量配方预测",
    r.status_code == 200 and len(data["results"]) == 2,
    f"总预测={data['total_predictions']} 耗时={data['elapsed_ms']}ms",
)
ok &= check("预测收敛", all(x["converged"] for x in data["results"]))

# 3. 实测验证闭环
records = client.get("/api/recipes", params={"process_id": pid}).json()
rid = next(x["id"] for x in records if x["label"] == "专色红")
pred = next(x for x in records if x["id"] == rid)["predicted_lab"]
r = client.post(
    "/api/recipes/verify",
    json={"recipe_id": rid, "measured_lab": [pred[0] + 3.0, pred[1] - 2.0, pred[2] + 1.5]},
)
v = r.json()
ok &= check(
    "实测验证闭环",
    r.status_code == 200 and v["improved"] and v["delta_e_corrected"] < v["delta_e_measured"],
    f"实测ΔE={v['delta_e_measured']} 修正后={v['delta_e_corrected']}",
)

# 4. CMYK→LAB 正查
r = client.post("/api/recipes/forward", json={"process_id": pid, "values": [0, 100, 100, 0]})
ok &= check(
    "CMYK→LAB 正查",
    r.status_code == 200 and r.json()["hex"].startswith("#"),
    f"{r.json()['lab']} {r.json()['hex']}",
)

# 5. 印前预检 + 转换
buf = io.BytesIO()
Image.new("RGB", (48, 48), (180, 130, 85)).save(buf, format="PNG")
b64 = base64.b64encode(buf.getvalue()).decode()
r = client.post("/api/files/preflight", json={"image_b64": b64, "filename": "e2e.png"})
rep = r.json()
ok &= check("印前预检（无嵌入 ICC → warn）", r.status_code == 200 and rep["status"] == "warn" and rep["size"] == [48, 48])

r = client.post(
    "/api/files/convert",
    json={"image_b64": b64, "process_id": pid, "intent": "relative_colorimetric"},
)
c = r.json()
ok &= check(
    "印前转换（CMYK TIFF）",
    r.status_code == 200 and c["output_mode"] == "CMYK" and c["file_format"] == "TIFF",
    f"预览{len(c['preview_b64']) // 1024}KB 成品{len(c['file_b64']) // 1024}KB",
)

# 6. 工具箱接口
r = client.post("/api/delta-e/lab", json={"lab1": [50, 20, 5], "lab2": [52, 18, 7]})
ok &= check("LAB 色差", r.status_code == 200 and r.json()["delta_e"] > 0, f"ΔE={r.json()['delta_e']}")
r = client.get("/api/visualization/gamut", params={"spaces": "srgb,dci-p3"})
ok &= check("色域可视化", r.status_code == 200 and "gamut_polygons" in r.json())

# 6.5 V0.4：gamut 新字段 / three.js 静态资产
r = client.get(f"/api/processes/{pid}/gamut?levels=6")
g = r.json()
ok &= check("gamut grid+surface 字段", g.get("grid", {}).get("channels") == 4 and bool(g.get("surface", {}).get("triangles")), f"三角面={len(g.get('surface', {}).get('triangles', []))}")
for path in ("/static/js/gamut-three.js", "/static/vendor/three/three.module.js", "/static/js/views/monitor.js"):
    r = client.get(path)
    ok &= check(f"V0.4 静态资产 {path}", r.status_code == 200)

# 6.6 V0.4：补偿模型沉淀（一次有效验证 → n=1）
r = client.get(f"/api/processes/{pid}")
comp = r.json().get("compensation")
ok &= check("补偿模型沉淀 n=1", bool(comp) and comp["n"] == 1 and any(v != 0 for v in comp["bias_device"]))

# 6.7 V0.4：监控 stats 聚合
r = client.get("/api/recipes/stats")
s = r.json()
ok &= check(
    "监控 stats 聚合",
    r.status_code == 200 and s["total"] >= 2 and len(s["series"]) == s["total"] and s["converged_rate"] == 1.0,
    f"total={s['total']} mean={s['mean_de']} p95={s['p95_de']}",
)
created = [p["created_at"] for p in s["series"]]
ok &= check("stats 时间序列升序", created == sorted(created))

# 6.8 V0.4：色域对比（同 ICC 双工艺 → 交集重合）
icc2 = (WIN / "RSWOP.icm").read_bytes()
r = client.post("/api/processes", params={"name": "E2E-SWOP-B", "filename": "RSWOP.icm"}, content=icc2, headers={"Content-Type": "application/octet-stream"})
pid_b = r.json()["id"]
r = client.get(f"/api/processes/{pid}/gamut/compare", params={"other": pid_b, "levels": 5})
cmp = r.json()
ok &= check(
    "色域对比（同 ICC 重合）",
    r.status_code == 200 and cmp["area_inter_over_a"] > 0.99 and len(cmp["inter_poly"]) >= 3,
    f"交集/A={cmp['area_inter_over_a']}",
)

# 6.9 V0.4：PDF 预检
pbuf = io.BytesIO()
Image.new("RGB", (420, 595), (120, 60, 160)).save(pbuf, format="PDF")
pb64 = base64.b64encode(pbuf.getvalue()).decode()
r = client.post("/api/files/preflight", json={"image_b64": pb64, "filename": "e2e.pdf"})
prep = r.json()
ok &= check(
    "PDF 预检（页数/输出意图告警/缩略图）",
    r.status_code == 200 and prep["format"] == "PDF" and prep["pages"] == 1
    and prep["pdf"]["has_output_intent"] is False and (prep["thumbnail_b64"] or "").startswith("data:image/png"),
    f"status={prep['status']}",
)

# 6.9.1 V0.9 P0-2：PDF/X-4 结构封装（Pillow PDF 为 RGB，宽松模式应生成并告警）
r = client.post(
    "/api/files/pdfx",
    params={"process_id": pid, "filename": "e2e.pdf", "strict": "0"},
    content=pbuf.getvalue(),
    headers={"Content-Type": "application/pdf"},
)
px = r.json()
ok &= check(
    "PDF/X-4 封装（宽松模式）",
    r.status_code == 200 and px["mode"] == "PDF/X-4"
    and px["output_intent"]["channels"] == 4 and px["status"] in ("pass", "warn"),
    f"status={px.get('status')} checks={len(px.get('checks', []))}",
)
from pypdf import PdfReader  # noqa: E402

_root = PdfReader(io.BytesIO(base64.b64decode(px["file_b64"].split(",", 1)[1]))).trailer["/Root"]
_oi = _root["/OutputIntents"][0].get_object()
_xmp = _root["/Metadata"].get_object().get_data().decode("utf-8", "replace")
ok &= check(
    "PDF/X-4 OutputIntent + XMP",
    str(_oi["/S"]) == "/GTS_PDFX"
    and int(_oi["/DestOutputProfile"].get_object()["/N"]) == 4
    and "PDF/X-4" in _xmp,
)
r = client.post(
    "/api/files/pdfx",
    params={"process_id": pid, "strict": "1"},
    content=pbuf.getvalue(),
    headers={"Content-Type": "application/pdf"},
)
ok &= check("PDF/X-4 严格模式拒绝 RGB 图像", r.status_code == 422, r.text[:80])

# 6.9.2 V0.9 P0-4：显示器档案 + 软打样门禁
from datetime import date as _date  # noqa: E402

_today = _date.today().isoformat()
r = client.post("/api/displays", json={
    "name": "E2E 显示器", "is_active": True,
    "targets": {"white_point": "D50", "luminance_cd_m2": 120, "contrast_ratio": 1000},
    "calibration": {
        "calibrated_at": _today, "valid_months": 6,
        "calibrator": "i1Display Pro", "profile_name": "e2e-display.icc",
    },
})
disp = r.json()
ok &= check(
    "显示器档案创建（校准有效）",
    r.status_code == 200 and disp["status"]["status"] == "ok" and disp["is_active"],
    f"status={disp.get('status', {}).get('status')}",
)
r = client.get("/api/displays/active-status")
ok &= check("软打样门禁 active-status 可信", r.status_code == 200 and r.json()["calibrated"] is True)
r = client.post("/api/files/convert", json={
    "image_b64": b64, "process_id": pid, "display_id": disp["id"],
})
ok &= check(
    "转换响应带软打样门禁",
    r.status_code == 200 and r.json()["softproof"]["status"] == "ok",
    f"softproof={r.json().get('softproof', {}).get('status') if r.status_code == 200 else r.text[:60]}",
)
E2E_DISPLAY_CGATS = b"""CGATS.17
NUMBER_OF_FIELDS 4
NUMBER_OF_SETS 2
BEGIN_DATA_FORMAT
SAMPLE_NAME XYZ_X XYZ_Y XYZ_Z
END_DATA_FORMAT
BEGIN_DATA
White 95.05 100.0 108.90
Black 0.0100 0.0100 0.0100
END_DATA
"""
r = client.post(
    f"/api/displays/{disp['id']}/measurements",
    params={"filename": "e2e-screen.cgats", "white_luminance_cd_m2": 120},
    content=E2E_DISPLAY_CGATS,
    headers={"Content-Type": "text/plain"},
)
ok &= check(
    "屏幕测量导入（CGATS XYZ → 白点/对比度）",
    r.status_code == 200
    and r.json()["measurement"]["xyz_patches"] == 2
    and abs(r.json()["measurement"]["white_xy"][0] - 0.3127) < 0.001
    and r.json()["calibration"]["contrast_ratio"] == 10000.0,
    r.text[:80] if r.status_code != 200 else "",
)
r = client.get(f"/api/displays/{disp['id']}/measurements/latest")
ok &= check(
    "屏幕测量原样导出（sha256 一致）",
    r.status_code == 200 and r.content == E2E_DISPLAY_CGATS
    and r.headers.get("x-content-sha256") == hashlib.sha256(E2E_DISPLAY_CGATS).hexdigest()[:16],
)
r = client.post("/api/displays", json={
    "name": "E2E 过期显示器", "is_active": False,
    "calibration": {"calibrated_at": "2020-01-01", "valid_months": 6},
})
expired = r.json()
ok &= check(
    "过期校准 → 不可信",
    r.status_code == 200 and expired["status"]["status"] == "expired"
    and expired["status"]["calibrated"] is False,
)
client.post(f"/api/displays/{expired['id']}/activate")
r = client.get("/api/displays/active-status")
ok &= check("过期显示器 active-status 拦截", r.json()["status"] == "expired")
r = client.get(f"/api/displays/{disp['id']}/report")
ok &= check("显示器校准报告导出", r.status_code == 200 and r.json()["status"]["status"] == "ok")
for did in (disp["id"], expired["id"]):
    client.delete(f"/api/displays/{did}")

# 6.10 V0.4：补偿模型重置
r = client.delete(f"/api/processes/{pid}/compensation")
ok &= check("补偿模型重置", r.status_code == 200 and r.json()["status"] == "reset")

# 7. V0.5：测量导入 + 可靠性分级（verified / rejected / 门禁）
E2E_SID = "E2E_QC_SESSION"


def _spectral_text(perturb: float = 0.0) -> bytes:
    fields = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in range(380, 731, 10)])
    import math

    def row(label, base):
        vals = [round(min(1.0, base + 0.35 / (1 + math.exp(-(w - 520) / 40))) + perturb, 4)
                for w in range(380, 731, 10)]
        return f"1\t{label}\t" + "\t".join(str(v) for v in vals)

    head = (
        'INSTRUMENTATION\t"Eye-One Pro"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        "NUMBER_OF_FIELDS\t38\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        "NUMBER_OF_SETS\t2\nBEGIN_DATA\n"
    )
    return (head + row("A1", 0.05) + "\n" + row("A2", 0.02) + "\nEND_DATA\n").encode("utf-8")


qc_params = {"filename": "e2e_v1.txt", "instrument": "E2E-Probe", "condition": "M1",
             "calibrated_at": "2026-01-01T08:00:00", "repeat_mode": "single", "process_id": pid}
r = client.post("/api/qc/measurements/import", params=qc_params, content=_spectral_text(),
                headers={"Content-Type": "application/octet-stream"})
ok &= check(
    "V0.5 测量导入（单次→warning，校准登记生效）",
    r.status_code == 200 and r.json()["record"]["reliability"] == "warning"
    and r.json()["session"]["instrument"] == "E2E-Probe",
    str(r.json().get("record", {}).get("reasons", []))[:60],
)
qc_sid = r.json()["session"]["id"]
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_v2.txt", "session_id": qc_sid, "repeat_mode": "repeat"},
                content=_spectral_text(), headers={"Content-Type": "application/octet-stream"})
ok &= check(
    "V0.5 复测对齐 → 会话 verified",
    r.status_code == 200 and r.json()["session"]["stats"]["verified"] == 2,
    str(r.json()["session"]["stats"]),
)

garbage = ("\n".join([
    'INSTRUMENTATION\t"Eye-One Pro"', "NUMBER_OF_FIELDS\t11",
    "BEGIN_DATA_FORMAT",
    "SampleID\tSAMPLE_NAME\tRGB_R\tRGB_G\tRGB_B\tXYZ_X\tXYZ_Y\tXYZ_Z\tLAB_L\tLAB_A\tLAB_B",
    "END_DATA_FORMAT", "NUMBER_OF_SETS\t1", "BEGIN_DATA",
    "1\tA1\t0\t0\t0\t-22.69\t-23.75\t-26.41\t0\t8.58\t305.27",
    "END_DATA"]) + "\n").encode("utf-8")
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_bad.txt", "session_id": qc_sid},
                content=garbage, headers={"Content-Type": "application/octet-stream"})
ok &= check(
    "V0.5 垃圾数据 → rejected",
    r.status_code == 200 and r.json()["record"]["reliability"] == "rejected",
    str(r.json()["record"]["reasons"])[:60],
)

r = client.get("/api/qc/measurements")
ok &= check("V0.5 会话列表", r.status_code == 200 and any(s["id"] == qc_sid for s in r.json()))
r = client.get("/api/qc/measurements", params={"session_id": qc_sid, "reliability": "rejected"})
ok &= check("V0.5 会话详情按可靠性过滤", r.status_code == 200 and len(r.json()["records"]) == 1)

# V0.5：ink-quality 只消费 verified
r = client.get("/api/qc/ink-quality", params={"session_id": qc_sid})
iq = r.json()
ok &= check(
    "V0.5 ink-quality 门禁（本会话无密度数据 → 空指标不误报）",
    r.status_code == 200 and iq["verified_records"] == 2 and iq["inks"] == {},
    f"verified={iq['verified_records']} skipped={iq['skipped_records']}",
)

# V0.5：testform 样张
r = client.post("/api/qc/testform", params={"process_id": pid})
tf = r.json()
ok &= check(
    "V0.5 验证样张（双点重复 + PDF/TIFF）",
    r.status_code == 200 and tf["pairs_count"] > 30 and tf["pdf_b64"][:4] == "JVBE"
    and len(tf["tiff_b64"]) > 10000,
    f"labels={tf['labels_count'] if 'labels_count' in tf else len(tf['labels'])} pairs={tf['pairs_count']}",
)

# V0.5：补偿门禁（关联无 verified 的会话 → 不沉淀）
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_single.txt", "instrument": "E2E-Probe",
                        "calibrated_at": "2026-01-01T09:00:00", "repeat_mode": "single"},
                content=_spectral_text(), headers={"Content-Type": "application/octet-stream"})
gate_sid = r.json()["session"]["id"]
ok &= check("V0.5 单次会话无 verified（门禁前置）", r.json()["session"]["stats"]["verified"] == 0)

records = client.get("/api/recipes", params={"process_id": pid}).json()
rid2 = next(x["id"] for x in records if x["label"] == "青")
pred2 = next(x for x in records if x["id"] == rid2)["predicted_lab"]
r = client.post(
    "/api/recipes/verify",
    json={"recipe_id": rid2, "measured_lab": [pred2[0] + 2.5, pred2[1] - 2.5, pred2[2] + 2.0],
          "measurement_session_id": gate_sid},
)
v = r.json()
ok &= check(
    "V0.5 补偿门禁（rejected 会话→修正照常、不沉淀）",
    r.status_code == 200 and v["improved"] is True
    and v["compensation_status"]["applied"] is False and v["compensation_status"]["skipped_reason"],
    f"components dL={v['components']['dL']}",
)
ok &= check("V0.5 verify 响应含 ΔE 分量", "components" in v and "dE76" in v["components"]
            and abs(v["components"]["dL"] - 2.5) < 0.5)

# V0.5 清理 QC 会话
for sid in (qc_sid, gate_sid):
    r = client.delete("/api/qc/measurements", params={"session_id": sid})
    ok &= check("V0.5 测量会话清理", r.status_code == 200 and r.json()["status"] == "deleted")

# 7.5 V0.6：K 值/叠印率/灰平衡/均匀性 + 诊断 + 对比/趋势 + 主观评价


def _density_tsv(c_solid=1.35, c75=0.90, b_over_m=1.15, gb="51.5\t0.8\t-1.2"):
    """合成密度 TSV（无 XYZ/Lab，GB1 带 Lab）：实地/梯尺(50/75)/叠印/灰平衡块。

    数值口径：M 实地 1.40 → T=(b_over_m−0.45)/1.40；K=(1.35−c75)/1.35。
    """
    head = "Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"
    rows = [
        f"C\t\t\t\t{c_solid}\t0.45\t0.12\t0.05",
        "M\t\t\t\t0.10\t1.40\t0.55\t0.04",
        "Y\t\t\t\t0.05\t0.10\t1.10\t0.03",
        "K\t\t\t\t1.60\t1.55\t1.45\t1.60",
        "c50\t\t\t\t0.45\t0.20\t0.07\t0.15",
        f"c75\t\t\t\t{c75}\t0.22\t0.06\t0.15",
        "k75\t\t\t\t0.90\t0.85\t0.80\t0.88",
        "R\t\t\t\t0.10\t1.35\t1.30\t0.40",
        "G\t\t\t\t1.05\t0.30\t1.02\t0.30",
        f"B\t\t\t\t1.20\t{b_over_m}\t0.30\t0.45",
        f"GB1\t{gb}\t1.05\t0.62\t0.61\t0.05",
    ]
    return (head + "\n".join(rows) + "\r\n").encode("utf-8")


def _import_density(client, filename, values, process_id=""):
    """导入同内容两份（密度复测对）→ 返回 (session_id, stats)。"""
    params = {"filename": filename, "instrument": "E2E-Dens",
              "calibrated_at": "2026-01-01T10:00:00", "repeat_mode": "single"}
    if process_id:
        params["process_id"] = process_id
    r = client.post("/api/qc/measurements/import", params=params, content=_density_tsv(**values),
                    headers={"Content-Type": "application/octet-stream"})
    sid = r.json()["session"]["id"]
    r = client.post("/api/qc/measurements/import", params={"filename": f"{filename}.r2", "session_id": sid,
                    "repeat_mode": "repeat"}, content=_density_tsv(**values),
                    headers={"Content-Type": "application/octet-stream"})
    return sid, r.json()["session"]["stats"]


# 问题会话：K≈0.33（低于 0.40）+ B 叠印 50%（低于 60%）+ 灰平衡 b* 偏蓝
v6_a, stats_a = _import_density(client, "e2e_v6a.txt", {}, process_id=pid)
ok &= check(
    "V0.6 纯密度复测配对 → verified",
    stats_a["verified"] == 2 and stats_a["warning"] == 0,
    str(stats_a),
)
r = client.get("/api/qc/ink-quality", params={"session_id": v6_a})
iq6 = r.json()
k_c = (iq6.get("print_contrast") or {}).get("C") or {}
ok &= check(
    "V0.6 反差率 K 聚合（Ds−D75/Ds）",
    r.status_code == 200 and abs(k_c.get("k", 0) - (1.35 - 0.90) / 1.35) < 1e-3
    and len(iq6.get("uniformity") or []) >= 1,
    f"k={k_c.get('k')} uni={len(iq6.get('uniformity') or [])}",
)
diag = iq6.get("diagnosis") or []
diag_rules = {f["rule"]: f["severity"] for f in diag}
ok &= check(
    "V0.6 诊断结论 R2 warn / R4 err / R5 warn",
    diag_rules.get("R2") == "warn" and diag_rules.get("R4") == "err" and diag_rules.get("R5") == "warn",
    str(diag_rules),
)
ok &= check(
    "V0.6 叠印率与灰平衡输出",
    any(t["label"] == "B" and abs(t["t"] - 50.0) < 0.2 for t in iq6.get("trapping") or [])
    and any(abs(g["db"] + 1.2) < 1e-6 for g in iq6.get("gray_balance") or []),
    str((iq6.get("trapping") or [{}])[0]),
)

# 健康会话：K≈0.44 + 叠印 71% 达标 + 灰平衡正常
v6_b, _ = _import_density(client, "e2e_v6b.txt",
                          {"c75": 0.75, "b_over_m": 1.45, "gb": "51.5\t0.3\t-0.5"}, process_id=pid)
r = client.get("/api/qc/compare", params={"session_id_a": v6_a, "session_id_b": v6_b})
cmp6 = r.json()
d_kc = (cmp6.get("deltas") or {}).get("k", {}).get("C") or {}
ok &= check(
    "V0.6 会话对比 Δ 表（K/B 叠印，Δ=主−对比）",
    r.status_code == 200 and abs(d_kc.get("delta", 0) - (0.3333 - 0.4444)) < 0.01,
    f"k.C delta={d_kc.get('delta')}",
)
r = client.get("/api/qc/trend", params={"process_id": pid})
pts6 = r.json().get("points") or []
ok &= check(
    "V0.6 工艺趋势时间序列",
    r.status_code == 200 and len(pts6) == 2 and all((p.get("k") or {}).get("C") is not None for p in pts6),
    f"points={len(pts6)}",
)

# 主观评价：模板 → 创建 → 双人打分 → 统计 → 非法打分 422 → 删除
r = client.get("/api/qc/evaluations/templates")
ok &= check(
    "V0.6 评价模板三套",
    r.status_code == 200 and set((r.json().get("templates") or {}).keys()) == {"book", "paper_pack", "plastic_pack"},
)
r = client.post("/api/qc/evaluations", params={"template": "paper_pack", "title": "E2E 评价"})
ev6 = r.json()
ev_id = ev6["id"]
ok &= check("V0.6 创建评价", r.status_code == 200 and ev6["template_name"] == "纸包装")
scores = {"颜色无明显偏色": 5, "色彩鲜艳饱满": 4, "色彩过渡自然": 4, "明暗部细节完整": 5,
          "阶调过渡平顺": 4, "图案边缘顺滑无毛刺": 5, "小字细线清晰不发虚": 4,
          "墨色均匀无深浅差异": 5, "墨层洁净无脏点晕染": 4, "裁切整齐无毛边": 5,
          "套印精准无错位": 4, "粘合牢固无开胶": 5, "盒体规整无歪斜": 5}
r = client.post("/api/qc/evaluations/entries", params={"evaluation_id": ev_id, "evaluator": "甲"},
                json=scores)
r2 = client.post("/api/qc/evaluations/entries", params={"evaluation_id": ev_id, "evaluator": "乙"},
                 json={**scores, "颜色无明显偏色": 4, "裁切整齐无毛边": 4})
st6 = r2.json().get("stats") or {}
ok &= check(
    "V0.6 评价统计（均值/标准差/综合）",
    r.status_code == 200 and r2.status_code == 200 and st6.get("entries") == 2
    and st6.get("overall") is not None and st6.get("consistency_warn") == [],
    f"overall={st6.get('overall')}",
)
r = client.post("/api/qc/evaluations/entries", params={"evaluation_id": ev_id, "evaluator": "丙"},
                json={"不存在指标": 5})
ok &= check("V0.6 非法指标打分 → 422", r.status_code == 422)
r = client.get("/api/qc/evaluations", params={"evaluation_id": ev_id})
ok &= check("V0.6 评价详情含统计", r.status_code == 200 and "stats" in r.json()
            and any(row["item"] == "裁切整齐无毛边" for row in r.json()["stats"]["rows"]))
r = client.delete("/api/qc/evaluations", params={"evaluation_id": ev_id})
ok &= check("V0.6 评价删除", r.status_code == 200 and r.json()["status"] == "deleted")

# V0.6 清理 QC 会话
for sid in (v6_a, v6_b):
    r = client.delete("/api/qc/measurements", params={"session_id": sid})
    ok &= check("V0.6 测量会话清理", r.status_code == 200 and r.json()["status"] == "deleted")

# 7.6 V0.7：校准版 → 双基准报告 → 建议应用 → 导出
r = client.post("/api/qc/calibration/form", params={"process_id": pid, "dpi": 300})
ok &= check("V0.7 校准版生成（28 色块单页）", r.status_code == 200
            and len(r.json()["patches"]) == 28 and r.json()["pages"] == 1
            and r.json()["pairs_count"] == 28, r.text[:80])
cal_form = r.json()
ok &= check("V0.7 参考表（ISO 涂布列仅固体+CMY）",
            cal_form["reference"]["iso_available"]
            and sum(1 for p in cal_form["reference"]["patches"] if p["iso_ref_lab"]) == 5
            and "CMYK_C" in cal_form["reference"]["reference_cgats"])

exp7 = {p["label"]: p["expected_lab"] for p in cal_form["reference"]["patches"]}
_j = {k: [v[0] + 0.3, v[1] - 0.2, v[2] + 0.1] for k, v in exp7.items()}
_tsv7 = ("Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"
         + "".join(f"{lb}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}\t\t\t\t\r\n"
                   for lb, lab in _j.items())).encode("utf-8")
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_v7.txt", "instrument": "E2E-色差仪", "condition": "M1",
                        "calibrated_at": "2026-01-01T10:00:00", "repeat_mode": "single",
                        "process_id": pid}, content=_tsv7,
                headers={"Content-Type": "application/octet-stream"})
v7_sid = r.json()["session"]["id"]
client.post("/api/qc/measurements/import",
            params={"filename": "e2e_v7.r2", "session_id": v7_sid, "repeat_mode": "repeat"},
            content=_tsv7, headers={"Content-Type": "application/octet-stream"})

r = client.get("/api/qc/calibration", params={"session_id": v7_sid})
rep7 = r.json()
ok &= check("V0.7 校准报告配对 28/28", r.status_code == 200 and rep7["matched"] == 28
            and rep7["overall"] in ("ok", "suggest", "needed"), r.text[:80])
ok &= check("V0.7 双基准输出（ΔE ICC + ΔE ISO + 分量）",
            all(("de_expected" in p and "comps" in p) for p in rep7["patches"])
            and sum(1 for p in rep7["patches"] if p["de_iso"] is not None) == 5)
ok &= check("V0.7 逐通道建议 4 条（含方向）",
            len(rep7["ink_suggestions"]) == 4
            and all("delta_pct" in s for s in rep7["ink_suggestions"]))
ok &= check("V0.7 校正曲线 4 通道 × 6 档（端点固定）",
            len(rep7["curves"]) == 4
            and all(len(c) == 6 and c[0]["corrected"] == 0.0 and c[-1]["corrected"] == 100.0
                    for c in rep7["curves"].values()))
ok &= check("V0.7 导出三件套（曲线 CSV / 实测 / 校正后 CGATS）",
            rep7["exports"]["curves_csv"].startswith("channel,t_nominal")
            and "NUMBER_OF_SAMPLES\t28" in rep7["exports"]["characterization_cgats"]
            and "NUMBER_OF_SAMPLES\t28" in rep7["exports"]["corrected_cgats"])
ok &= check("V0.7 曲线手算锚点（t=0 恒等、单调）",
            all(e["t"] <= e2["t"] and e["corrected"] <= e2["corrected"]
                for c in rep7["curves"].values()
                for e, e2 in zip(c, c[1:])),
            str({k: [(e['t'], e['corrected']) for e in v] for k, v in rep7['curves'].items()})[:120])

r = client.post("/api/qc/calibration/apply", params={"session_id": v7_sid})
applied = r.json()
ok &= check("V0.7 建议应用 → 补偿模型（n≥2，预测联动）",
            r.status_code == 200 and applied["applied"] and applied["n"] >= 2
            and len(applied["bias_device"]) == 4, r.text[:80])
r = client.get(f"/api/processes/{pid}")
ok &= check("V0.7 工艺详情返回补偿偏置",
            r.status_code == 200 and (r.json().get("compensation") or {}).get("bias_device") == applied["bias_device"])

# 守卫：未绑定工艺会话 → 422；非 CMYK 工艺校准版 → 422（内联最小合法 GRAY ICC，同 conftest 构造）
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_v7_nobind.txt", "instrument": "E2E-色差仪",
                        "calibrated_at": "2026-01-01T10:00:00", "repeat_mode": "single"},
                content=_tsv7, headers={"Content-Type": "application/octet-stream"})
nobind_sid = r.json()["session"]["id"]
r = client.get("/api/qc/calibration", params={"session_id": nobind_sid})
ok &= check("V0.7 未绑定工艺 → 422", r.status_code == 422, r.text[:60])


def _make_gray_icc(gamma: float = 2.2) -> bytes:
    import struct

    hdr = bytearray(128)
    struct.pack_into(">I", hdr, 8, 0x04300000)
    hdr[12:16] = b"mntr"
    hdr[16:20] = b"GRAY"
    hdr[20:24] = b"XYZ "
    hdr[24:36] = struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
    hdr[36:40] = b"acsp"
    struct.pack_into(">i", hdr, 60, 0x0000F6D6)
    struct.pack_into(">i", hdr, 64, 0x00010000)
    struct.pack_into(">i", hdr, 68, 0x0000D32D)
    curve = b"curv" + struct.pack(">II", 0, 1) + struct.pack(">H", int(gamma * 256)) + b"\x00\x00"
    xyz = b"XYZ " + struct.pack(">I", 0) + struct.pack(">iii", 0x0000F6D6, 0x00010000, 0x0000D32D)
    text = b"E2E Gray\x00"
    mluc = b"mluc" + struct.pack(">II", 0, 1) + struct.pack(">HHII", 0x656E, 0x5553, len(text), 24) + text
    body = struct.pack(">I", 3)
    offset = 128 + len(body) + 12 * 3
    entries = b""
    data = b""
    for sig, d in ((b"desc", mluc), (b"wtpt", xyz), (b"kTRC", curve)):
        entries += sig + struct.pack(">II", offset, len(d))
        data += d
        offset += len(d)
    blob = bytes(hdr) + body + entries + data
    return struct.pack(">I", len(blob)) + blob[4:]


r = client.post("/api/processes", params={"name": "E2E-GRAY-校准守卫", "filename": "e2e_gray.icc"},
                content=_make_gray_icc(), headers={"Content-Type": "application/octet-stream"})
gray_pid = r.json()["id"]
r = client.post("/api/qc/calibration/form", params={"process_id": gray_pid})
ok &= check("V0.7 非 CMYK 工艺校准版 → 422", r.status_code == 422, r.text[:60])
r = client.delete(f"/api/processes/{gray_pid}")
ok &= check("V0.7 灰度守卫工艺清理", r.status_code == 200)

# 7.7 V0.8：校准轮次落库 → 历史收敛 → 新会话对比上一轮
r = client.get("/api/qc/calibration/history", params={"process_id": pid})
h8 = r.json()
ok &= check("V0.8 V0.7 轮次已落库（apply 即存档）",
            r.status_code == 200 and h8["round_no"] == 1
            and h8["rounds"][0]["session_id"] == v7_sid
            and h8["convergence"]["status"] == "n/a", r.text[:100])

# 改善会话：抖动减半 → 报告自带 prev_round_compare → apply verdict=improved
_j2 = {k: [v[0] + 0.1, v[1] - 0.05, v[2] + 0.05] for k, v in exp7.items()}
_tsv8 = ("Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"
         + "".join(f"{lb}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}\t\t\t\t\r\n"
                   for lb, lab in _j2.items())).encode("utf-8")
r = client.post("/api/qc/measurements/import",
                params={"filename": "e2e_v8.txt", "instrument": "E2E-色差仪", "condition": "M1",
                        "calibrated_at": "2026-01-01T10:00:00", "repeat_mode": "single",
                        "process_id": pid}, content=_tsv8,
                headers={"Content-Type": "application/octet-stream"})
v8_sid = r.json()["session"]["id"]
client.post("/api/qc/measurements/import",
            params={"filename": "e2e_v8.r2", "session_id": v8_sid, "repeat_mode": "repeat"},
            content=_tsv8, headers={"Content-Type": "application/octet-stream"})
rep8 = client.get("/api/qc/calibration", params={"session_id": v8_sid}).json()
cmp8 = rep8.get("prev_round_compare") or {}
ok &= check("V0.8 新会话报告自带较上轮判定",
            cmp8.get("verdict") == "improved"
            and cmp8.get("prev_max_de_expected") is not None
            and cmp8["prev_max_de_expected"] > rep8["summary"]["max_de_expected"],
            f"verdict={cmp8.get('verdict')} prev={cmp8.get('prev_max_de_expected')} cur={rep8['summary']['max_de_expected']}")
ok &= check("V0.8 轮次对比含逐通道 Δ",
            any(x.get("d") is not None for x in cmp8.get("per_channel", [])))
r = client.post("/api/qc/calibration/apply", params={"session_id": v8_sid})
a8 = r.json()
ok &= check("V0.8 第二轮 apply（round_no=2，verdict=improved）",
            r.status_code == 200 and a8["round_no"] == 2
            and a8["verdict"]["verdict"] == "improved", r.text[:100])
r = client.get("/api/qc/calibration/history", params={"process_id": pid})
h8 = r.json()
ok &= check("V0.8 两轮历史 + 收敛判定（趋势/收敛）",
            h8["round_no"] == 2 and len(h8["trend"]) == 2
            and h8["convergence"]["status"] in ("converged", "improving")
            and h8["convergence"]["rounds"] == 2, r.text[:120])

# V0.8 清理（轮次随工艺删除级联，见第 8 节）
for sid in (v8_sid,):
    r = client.delete("/api/qc/measurements", params={"session_id": sid})
    ok &= check("V0.8 测量会话清理", r.status_code == 200 and r.json()["status"] == "deleted")

# 8. 清理
for p in (pid, pid_b):
    r = client.delete(f"/api/processes/{p}")
    ok &= check("清理工艺", r.status_code == 200)
r = client.get("/api/recipes", params={"process_id": pid})
ok &= check("配方记录清理", r.status_code == 200 and r.json() == [])

print()
print("E2E ALL OK" if ok else "E2E HAS FAILURES")
sys.exit(0 if ok else 1)
