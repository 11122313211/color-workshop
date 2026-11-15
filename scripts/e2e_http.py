"""HTTP 端到端验证：走通 工艺创建→配方预测→实测验证→预检→转换→清理。"""

import base64
import io
import sys
from pathlib import Path

import httpx
from PIL import Image

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
ok &= check("版本号 v0.6.0", r.json().get("version") == "0.6.0")
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

# 8. 清理
for p in (pid, pid_b):
    r = client.delete(f"/api/processes/{p}")
    ok &= check("清理工艺", r.status_code == 200)
r = client.get("/api/recipes", params={"process_id": pid})
ok &= check("配方记录清理", r.status_code == 200 and r.json() == [])

print()
print("E2E ALL OK" if ok else "E2E HAS FAILURES")
sys.exit(0 if ok else 1)
