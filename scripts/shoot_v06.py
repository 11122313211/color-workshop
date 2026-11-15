"""V0.6 视觉验收：隔离数据目录起服务 → API 造数 → Playwright 截图。

用法: python scripts/shoot_v06.py <输出目录>
产出: monitor-light-top/qc/compare/dark-qc.png / eval-card.png / eval-score.png /
      monitor-375.png / monitor-375-wheel.png / process-testform.png
造数特征（全部合成）：问题密度会话（TVI 偏高 err + K 低 warn + 叠印 50% err +
灰平衡偏色 warn）与健康会话（全达标）绑定同工艺 → 诊断结论 / 对比 Δ 表 / 趋势
sparkline 均有内容；主观评价双评价人打分。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="cw_v06_shots_"))
OUT.mkdir(parents=True, exist_ok=True)

DATA_DIR = tempfile.mkdtemp(prefix="cw_v06_data_")
os.environ["COLORFORGE_DATA_DIR"] = DATA_DIR

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
import threading  # noqa: E402

t = threading.Thread(target=server.run, daemon=True)
t.start()
time.sleep(2.5)

c = httpx.Client(base_url=BASE, timeout=60)

# ---- 造数：工艺 + 配方闭环 ----
icc = Path("tests/fixtures/icc/testbed_cmyk.icc").read_bytes()
r = c.post("/api/processes", params={"name": "铜版纸演示", "paper": "铜版纸", "ink": "四色", "filename": "testbed_cmyk.icc"},
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

# ---- V0.6 密度会话（无 Lab，GB1 除外）：问题会话 + 健康会话，同工艺 → 趋势 ----


def density_tsv(c_solid=1.35, c50=0.60, c75=0.92, b_over_m=1.15, gb="52.0\t2.2\t-1.6"):
    """合成密度 TSV。口径：K=(1.35−c75)/1.35；T=(b_over_m−0.45)/1.40。"""
    head = "Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"
    rows = [
        f"C\t\t\t\t{c_solid}\t0.45\t0.12\t0.05",
        "M\t\t\t\t0.10\t1.40\t0.55\t0.04",
        "Y\t\t\t\t0.05\t0.10\t1.10\t0.03",
        "K\t\t\t\t1.60\t1.55\t1.45\t1.60",
        f"c50\t\t\t\t{c50}\t0.20\t0.07\t0.15",
        f"c75\t\t\t\t{c75}\t0.22\t0.06\t0.15",
        "k75\t\t\t\t0.90\t0.85\t0.80\t0.88",
        "R\t\t\t\t0.10\t1.35\t1.30\t0.40",
        "G\t\t\t\t1.05\t0.30\t1.02\t0.30",
        f"B\t\t\t\t1.20\t{b_over_m}\t0.30\t0.45",
        f"GB1\t{gb}\t1.05\t0.62\t0.61\t0.05",
    ]
    return (head + "\n".join(rows) + "\r\n").encode("utf-8")


def import_density(filename, **values):
    r = c.post("/api/qc/measurements/import",
               params={"filename": filename, "instrument": "Xrite530 分光密度计", "condition": "M1",
                       "calibrated_at": "2026-07-08T09:30:00", "backing": "黑", "repeat_mode": "single",
                       "process_id": pid},
               content=density_tsv(**values), headers={"Content-Type": "application/octet-stream"})
    sid = r.json()["session"]["id"]
    c.post("/api/qc/measurements/import",
           params={"filename": f"{filename}.r2", "session_id": sid, "repeat_mode": "repeat"},
           content=density_tsv(**values), headers={"Content-Type": "application/octet-stream"})
    return sid


# 先导入健康会话，再导入问题会话（created_at 严格更新 → 倒序排最前 → QC 卡默认展示问题诊断）
healthy_sid = import_density("铜版纸-周期2-达标.txt", c_solid=1.45, c50=0.44, c75=0.85, b_over_m=1.45, gb="52.0\t0.3\t-0.5")
time.sleep(1.5)
problem_sid = import_density("铜版纸-周期3-异常.txt")

# ---- V0.5 风格 verified TSV 会话（Lab+XYZ+密度，GATF 卡/TVI/色轮有数据） ----
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
r = c.post("/api/qc/measurements/import",
           params={"filename": "哑粉纸-光谱组1.txt", "instrument": "Eye-One Pro", "condition": "M1",
                   "calibrated_at": "2026-07-06T09:30:00", "repeat_mode": "single", "process_id": pid},
           content=(TSV_HEAD + density_rows).encode("utf-8"),
           headers={"Content-Type": "application/octet-stream"})
lab_sid = r.json()["session"]["id"]
c.post("/api/qc/measurements/import",
       params={"filename": "哑粉纸-光谱组2.txt", "session_id": lab_sid, "repeat_mode": "repeat"},
       content=(TSV_HEAD + density_rows).encode("utf-8"), headers={"Content-Type": "application/octet-stream"})

# 单次 warning 会话（历史包袱样例）
c.post("/api/qc/measurements/import",
       params={"filename": "新闻纸-单次.txt", "instrument": "Eye-One Pro", "condition": "M0",
               "calibrated_at": "2026-07-05T14:00:00", "repeat_mode": "single"},
       content=(TSV_HEAD + density_rows).encode("utf-8"), headers={"Content-Type": "application/octet-stream"})

# ---- 主观评价：纸包装模板 + 双评价人 ----
r = c.post("/api/qc/evaluations", params={"template": "paper_pack", "title": "季度包装抽检 · 合成演示"})
ev_id = r.json()["id"]
scores = {"颜色无明显偏色": 5, "色彩鲜艳饱满": 4, "色彩过渡自然": 4, "明暗部细节完整": 5,
          "阶调过渡平顺": 4, "图案边缘顺滑无毛刺": 5, "小字细线清晰不发虚": 4,
          "墨色均匀无深浅差异": 5, "墨层洁净无脏点晕染": 4, "裁切整齐无毛边": 5,
          "套印精准无错位": 4, "粘合牢固无开胶": 5, "盒体规整无歪斜": 5}
c.post("/api/qc/evaluations/entries", params={"evaluation_id": ev_id, "evaluator": "评价人 07"},
       json=scores)
c.post("/api/qc/evaluations/entries", params={"evaluation_id": ev_id, "evaluator": "评价人 12"},
       json={**scores, "颜色无明显偏色": 4, "色彩过渡自然": 3, "小字细线清晰不发虚": 3})

iq = c.get("/api/qc/ink-quality", params={"session_id": problem_sid}).json()
print("seeded; problem diagnosis rules:",
      {f["rule"]: f["severity"] for f in iq.get("diagnosis", [])})

# ---- Playwright 截图 ----
from playwright.sync_api import sync_playwright  # noqa: E402


def scroll_main_to(page, selector: str, top_below: float = 100, attempts: int = 12):
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

    # 1) 监控页亮色：顶部 + QC 卡（诊断结论 + 新指标块 + K chip）
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(f"{BASE}/static/index.html#/monitor")
    pg.wait_for_selector(".qc-diagnosis", timeout=20000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(OUT / "monitor-light-top.png"), full_page=False)
    scroll_main_to(pg, ".qc-diagnosis")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 60")
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(OUT / "monitor-light-qc.png"), full_page=False)

    # 2) 会话对比：精确选健康会话 → Δ 表（问题 vs 达标，Δ 有内容）
    pg.select_option("#qc-ink-compare", value=healthy_sid)
    pg.wait_for_selector("#qc-compare-block", timeout=15000)
    pg.wait_for_timeout(1200)
    scroll_main_to(pg, "#qc-compare-block")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 60")
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(OUT / "monitor-light-compare.png"), full_page=False)

    # 3) 诊断结论展开态（点第一条 err/warn 行）
    pg.eval_on_selector(".qc-diagnosis > div:first-child", "el => el.click()")
    pg.wait_for_timeout(400)
    scroll_main_to(pg, ".qc-diagnosis")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 60")
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(OUT / "monitor-light-diagnosis.png"), full_page=False)

    # 4) 暗色 QC 卡
    pg.evaluate("localStorage.setItem('theme','dark'); document.documentElement.dataset.theme='dark'; window.dispatchEvent(new Event('themechange'))")
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "monitor-dark-qc.png"), full_page=False)
    pg.evaluate("localStorage.setItem('theme','light'); document.documentElement.dataset.theme='light'; window.dispatchEvent(new Event('themechange'))")

    # 5) 主观评价卡 + 打分模态
    scroll_main_to(pg, "#qc-eval-card")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 48")
    pg.wait_for_timeout(600)
    pg.screenshot(path=str(OUT / "eval-card.png"), full_page=False)
    pg.click(".qc-eval-score")
    pg.wait_for_selector(".ev-score-row", timeout=8000)
    # 点两行打分让模态有交互痕迹
    pg.eval_on_selector(".ev-score-row .ev-vote", "el => el.click()")
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(OUT / "eval-score.png"), full_page=False)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)

    # 6) 375px：QC 卡特写 + 色轮
    pg.close()
    m = browser.new_page(viewport={"width": 375, "height": 812})
    m.goto(f"{BASE}/static/index.html#/monitor")
    m.wait_for_selector(".qc-diagnosis", timeout=20000)
    m.wait_for_timeout(1200)
    scroll_main_to(m, "#qc-ink-card")
    m.wait_for_timeout(400)
    m.screenshot(path=str(OUT / "monitor-375.png"), full_page=False)
    scroll_main_to(m, "#qc-wheel-canvas", top_below=140)
    m.wait_for_timeout(400)
    m.screenshot(path=str(OUT / "monitor-375-wheel.png"), full_page=False)
    m.close()

    # 7) 工艺详情 → 生成样张（第二页目视诊断区在 TIFF/PDF 内，预览为第一页）
    d = browser.new_page(viewport={"width": 1280, "height": 900})
    d.goto(f"{BASE}/static/index.html#/processes/{pid}")
    d.wait_for_selector("#btn-testform", timeout=15000)
    d.click("#btn-testform")
    d.wait_for_selector(".tf-go", timeout=8000)
    d.click(".tf-go")
    d.wait_for_selector("#tf-result img", timeout=20000)
    d.wait_for_timeout(800)
    d.screenshot(path=str(OUT / "process-testform.png"), full_page=False)
    d.close()

    browser.close()

print("shots done ->", OUT)
server.should_exit = True
