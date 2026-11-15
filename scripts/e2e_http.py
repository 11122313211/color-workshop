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

# 7. 清理
r = client.delete(f"/api/processes/{pid}")
ok &= check("清理工艺", r.status_code == 200)
r = client.get("/api/recipes", params={"process_id": pid})
ok &= check("配方记录清理", r.status_code == 200 and r.json() == [])

print()
print("E2E ALL OK" if ok else "E2E HAS FAILURES")
sys.exit(0 if ok else 1)
