"""V0.5 视觉验收：隔离数据目录起服务 → API 造数 → Playwright 截图。

用法: python scripts/shoot_v05.py <输出目录>
产出: monitor-light.png / monitor-dark.png / monitor-375.png / qc-wizard.png / process-testform.png
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="cw_v05_shots_"))
OUT.mkdir(parents=True, exist_ok=True)

DATA_DIR = tempfile.mkdtemp(prefix="cw_v05_data_")
os.environ["COLORFORGE_DATA_DIR"] = DATA_DIR

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
import threading

t = threading.Thread(target=server.run, daemon=True)
t.start()
time.sleep(2.5)

c = httpx.Client(base_url=BASE, timeout=60)

# ---- 造数：工艺 + 配方闭环 + QC 会话 ----
icc = Path("tests/fixtures/icc/testbed_cmyk.icc").read_bytes()
r = c.post("/api/processes", params={"name": "哑粉纸演示", "paper": "哑粉纸", "ink": "四色", "filename": "testbed_cmyk.icc"},
           content=icc, headers={"Content-Type": "application/octet-stream"})
pid = r.json()["id"]

r = c.post("/api/recipes/predict", json={
    "process_id": pid,
    "targets": [
        {"label": "专色红", "lab": [50.0, 20.0, 5.0]},
        {"label": "青", "lab": [50.0, -20.0, -25.0]},
        {"label": "暖灰", "lab": [72.0, 2.0, 8.0]},
    ],
    "tolerance": 2.0,
})
records = c.get("/api/recipes", params={"process_id": pid}).json()
doc = next(x for x in records if x.get("label") == "专色红")
pred = doc["predicted_lab"]
c.post("/api/recipes/verify", json={"recipe_id": doc["id"],
                                    "measured_lab": [pred[0] + 2.2, pred[1] - 1.8, pred[2] + 1.2]})

# verified 密度会话（TSV×2 同内容 → 复测一致）+ 单次 warning 会话
from app.services.cgats import lab_from_xyz, white_xyz  # noqa: E402
import numpy as np  # noqa: E402

white = np.asarray(white_xyz("D50", "2"))


def tsv_row(label, scale, den):
    xyz = [float(v) for v in white * scale]
    lab = lab_from_xyz(xyz, white)
    return (f"{label}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}\t"
            f"{xyz[0]:.2f}\t{xyz[1]:.2f}\t{xyz[2]:.2f}\t"
            f"{np.hypot(lab[1], lab[2]):.2f}\t90.00\t210.00\t"
            f"{den[0]:.2f}\t{den[1]:.2f}\t{den[2]:.2f}\t{den[3]:.2f}\r\n")


TSV_HEAD = "Color Name\tCIE Lab\t\t\tCIE XYZ\t\t\tLab LCh\t\t\tStatus T C\tM\tY\tK\r\n"
density_rows = "".join([
    tsv_row("C", 0.09, [1.55, 0.43, 0.17, 0.78]),
    tsv_row("M", 0.22, [0.20, 1.56, 0.86, 0.60]),
    tsv_row("Y", 0.45, [0.02, 0.10, 1.09, 0.06]),
    tsv_row("c60", 0.30, [0.51, 0.19, 0.06, 0.34]),
    tsv_row("m30", 0.40, [0.05, 0.20, 0.15, 0.14]),
    tsv_row("y40", 0.60, [0.01, 0.03, 0.28, 0.02]),
    tsv_row("w", 1.00, [0.01, 0.01, 0.02, 0.01]),
])
tsv_bytes = (TSV_HEAD + density_rows).encode("utf-8")

r = c.post("/api/qc/measurements/import",
           params={"filename": "铜版纸-组1.txt", "instrument": "Xrite530 分光密度计", "condition": "M1",
                   "calibrated_at": "2026-07-08T09:30:00", "backing": "黑", "repeat_mode": "single",
                   "process_id": pid},
           content=tsv_bytes, headers={"Content-Type": "application/octet-stream"})
sid = r.json()["session"]["id"]
c.post("/api/qc/measurements/import",
       params={"filename": "铜版纸-组2.txt", "session_id": sid, "repeat_mode": "repeat"},
       content=tsv_bytes, headers={"Content-Type": "application/octet-stream"})

c.post("/api/qc/measurements/import",
       params={"filename": "新闻纸-单次.txt", "instrument": "Eye-One Pro", "condition": "M0",
               "calibrated_at": "2026-07-09T14:00:00", "repeat_mode": "single"},
       content=tsv_bytes, headers={"Content-Type": "application/octet-stream"})

# 垃圾 RGB 发射会话 → rejected
garbage = ("\n".join([
    'INSTRUMENTATION\t"Eye-One Pro"',
    'MEASUREMENT_SOURCE\t"Illumination=Emission  Filter=No"',
    "NUMBER_OF_FIELDS\t11",
    "BEGIN_DATA_FORMAT",
    "SampleID\tSAMPLE_NAME\tRGB_R\tRGB_G\tRGB_B\tXYZ_X\tXYZ_Y\tXYZ_Z\tLAB_L\tLAB_A\tLAB_B",
    "END_DATA_FORMAT", "NUMBER_OF_SETS\t1", "BEGIN_DATA",
    "1\tA1\t0\t0\t0\t-22.69\t-23.75\t-26.41\t0\t8.58\t305.27",
    "END_DATA"]) + "\n").encode("utf-8")
r = c.post("/api/qc/measurements/import",
           params={"filename": "屏幕发射-未校准.txt", "instrument": "Eye-One Pro (发射模式)",
                   "calibrated_at": "2026-07-10T10:00:00", "repeat_mode": "repeat"},
           content=garbage, headers={"Content-Type": "application/octet-stream"})
garbage_sid = r.json()["session"]["id"]
c.post("/api/qc/measurements/import",
       params={"filename": "屏幕发射-复测.txt", "session_id": garbage_sid, "repeat_mode": "repeat"},
       content=garbage, headers={"Content-Type": "application/octet-stream"})

print("seeded, sessions:", len(c.get("/api/qc/measurements").json()))

# ---- Playwright 截图 ----
from playwright.sync_api import sync_playwright  # noqa: E402


def scroll_main_to(page, selector: str, top_below: float = 100, attempts: int = 12):
    """应用滚动容器是 .main（内部滚动），且上方内容懒渲染会推挤位置——循环滚动直到稳定。"""
    for _ in range(attempts):
        page.eval_on_selector(selector, "el => el.scrollIntoView({block:'start', behavior:'instant'})")
        page.wait_for_timeout(350)
        top = page.eval_on_selector(selector, "el => el.getBoundingClientRect().top")
        if top is not None and top <= top_below:
            break


def shoot(page, path_, full=True):
    page.wait_for_timeout(1200)
    page.screenshot(path=str(path_), full_page=full)


with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:
        browser = pw.chromium.launch(headless=True, channel="msedge")

    # 1) 监控页（亮色，桌面）——顶部视口 + QC 卡视口特写
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(f"{BASE}/static/index.html#/monitor")
    pg.wait_for_selector("#qc-ink-card canvas", timeout=15000)
    pg.wait_for_timeout(1200)
    pg.screenshot(path=str(OUT / "monitor-light-top.png"), full_page=False)
    scroll_main_to(pg, "#qc-ink-card")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 48")
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(OUT / "monitor-light-qc.png"), full_page=False)

    # 2) 暗色（顶部视口 + QC 卡特写；应用为内部滚动容器，full_page 无法展开整页）
    pg.evaluate("localStorage.setItem('theme','dark'); document.documentElement.dataset.theme='dark'; window.dispatchEvent(new Event('themechange'))")
    pg.eval_on_selector(".main", "el => el.scrollTop = 0")
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "monitor-dark-top.png"), full_page=False)
    scroll_main_to(pg, "#qc-ink-card")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 48")
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(OUT / "monitor-dark-qc.png"), full_page=False)

    # 3) 导入向导模态（亮色恢复）
    pg.evaluate("localStorage.setItem('theme','light'); document.documentElement.dataset.theme='light'; window.dispatchEvent(new Event('themechange'))")
    pg.goto(f"{BASE}/static/index.html#/monitor")
    pg.wait_for_selector(".qc-import-btn", timeout=15000)
    pg.click(".qc-import-btn")
    pg.wait_for_selector("#qc-instrument", timeout=8000)
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(OUT / "qc-wizard.png"), full_page=False)

    # 4) 375px 监控页（QC 卡特写 + 色轮特写两段覆盖）
    pg.close()
    m = browser.new_page(viewport={"width": 375, "height": 812})
    m.goto(f"{BASE}/static/index.html#/monitor")
    m.wait_for_selector("#qc-ink-card canvas", timeout=15000)
    m.wait_for_timeout(1200)
    scroll_main_to(m, "#qc-ink-card")
    m.wait_for_timeout(400)
    m.screenshot(path=str(OUT / "monitor-375.png"), full_page=False)
    scroll_main_to(m, "#qc-wheel-canvas", top_below=140)
    m.wait_for_timeout(400)
    m.screenshot(path=str(OUT / "monitor-375-wheel.png"), full_page=False)
    m.close()

    # 5) 工艺详情 → 生成样张模态
    d = browser.new_page(viewport={"width": 1280, "height": 900})
    d.goto(f"{BASE}/static/index.html#/processes/{pid}")
    d.wait_for_selector("#btn-testform", timeout=15000)
    d.click("#btn-testform")
    d.wait_for_selector(".tf-go", timeout=8000)
    d.click(".tf-go")
    d.wait_for_selector("#tf-result img", timeout=20000)
    d.wait_for_timeout(800)
    d.screenshot(path=str(OUT / "process-testform.png"), full_page=False)
    # 展开双点配对坐标 details 并滚动到可见
    d.eval_on_selector("#tf-result details", "el => el.open = true")
    d.wait_for_timeout(400)
    d.eval_on_selector("#tf-result details", "el => el.scrollIntoView({block:'center'})")
    d.wait_for_timeout(400)
    d.screenshot(path=str(OUT / "process-testform-pairs.png"), full_page=False)

    browser.close()

print("shots done ->", OUT)
server.should_exit = True
