"""V0.7 视觉验收：隔离数据目录起服务 → API 造数 → Playwright 走查校准向导全流程。

用法: python scripts/shoot_v07.py <输出目录>
产出: cal-wizard-step1/2/3.png / cal-applied.png / monitor-cal-line.png /
      monitor-dark-cal.png / cal-wizard-375.png / process-detail.png
造数特征（全部合成）：铜版纸工艺（testbed ICC）→ 校准版参考表构造两遍复测
（微抖动 → overall ok）+ 第二会话（K 固体偏暗 → 建议 delta<0）。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="cw_v07_shots_"))
OUT.mkdir(parents=True, exist_ok=True)

DATA_DIR = tempfile.mkdtemp(prefix="cw_v07_data_")
os.environ["COLORFORGE_DATA_DIR"] = DATA_DIR

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
import threading  # noqa: E402

t = threading.Thread(target=server.run, daemon=True)
t.start()
time.sleep(2.5)

c = httpx.Client(base_url=BASE, timeout=120)

# ---- 造数：工艺 + 两个校准测量会话（合成，来自参考表 + 抖动/偏移） ----
icc = Path("tests/fixtures/icc/testbed_cmyk.icc").read_bytes()
r = c.post("/api/processes", params={"name": "铜版纸校准演示", "paper": "铜版纸", "ink": "四色",
                                     "filename": "testbed_cmyk.icc"},
           content=icc, headers={"Content-Type": "application/octet-stream"})
pid = r.json()["id"]

from app.services.calibration import build_reference  # noqa: E402

proc_doc = c.get("/api/processes").json()[0]
ref = build_reference(proc_doc)
exp = {p["label"]: p["expected_lab"] for p in ref["patches"]}

TSV_HEAD = "Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"


def tsv_from_labs(labs, p_index=False):
    rows = []
    for i, (label, lab) in enumerate(labs.items(), 1):
        tag = f"P{i:02d}" if p_index else label
        rows.append(f"{tag}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}")
    return (TSV_HEAD + "\r\n".join(rows) + "\r\n").encode()


def import_two(files, first_single=True):
    sid = ""
    for i, data in enumerate(files):
        if i == 0:
            q = {"filename": "cal-r1.txt", "instrument": "Xrite i1（校准演示）", "condition": "M1",
                 "calibrated_at": "2026-11-15T09:30:00", "repeat_mode": "single" if first_single else "repeat",
                 "process_id": pid}
        else:
            q = {"filename": "cal-r2.txt", "session_id": sid, "repeat_mode": "repeat"}
        r = c.post("/api/qc/measurements/import", params=q, content=data,
                   headers={"Content-Type": "application/octet-stream"})
        sid = r.json()["session"]["id"]
    return sid


# 会话 1：轻抖动（≈达标）——绑定工艺
jitter = {k: [v[0] + 0.4, v[1] + 0.3, v[2] - 0.2] for k, v in exp.items()}
sid_ok = import_two([tsv_from_labs(jitter), tsv_from_labs(jitter)])
# 会话 2：K 通道明显偏暗 + TVI 漂移（建议 delta<0、曲线有内容）
dark = {k: list(v) for k, v in exp.items()}
for label in list(dark):
    if label.startswith("K") and label[1:].isdigit():
        t = int(label[1:])
        dark[label] = [exp[label][0] - 4.0 * (t / 100.0) - 2.0, exp[label][1], exp[label][2]]
dark["K100"] = [exp["K100"][0] - 6.0, exp["K100"][1], exp["K100"][2]]
sid_dark = import_two([tsv_from_labs(dark), tsv_from_labs(dark)])
time.sleep(1.2)

rep = c.get("/api/qc/calibration", params={"session_id": sid_dark}).json()
print("dark session:", rep["overall"], "matched", rep["matched"],
      "K delta", next(s["delta_pct"] for s in rep["ink_suggestions"] if s["channel"] == "K"))
rep_ok = c.get("/api/qc/calibration", params={"session_id": sid_ok}).json()
print("ok session:", rep_ok["overall"], "max/avg", rep_ok["summary"]["max_de_expected"], rep_ok["summary"]["avg_de_expected"])

# ---- Playwright ----
from playwright.sync_api import sync_playwright  # noqa: E402


def scroll_main_to(page, selector: str, top_below: float = 100, attempts: int = 12):
    for _ in range(attempts):
        page.eval_on_selector(selector, "el => el.scrollIntoView({block:'start', behavior:'instant'})")
        page.wait_for_timeout(350)
        top = page.eval_on_selector(selector, "el => el.getBoundingClientRect().top")
        if top is not None and top <= top_below:
            break


with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:
        browser = pw.chromium.launch(headless=True, channel="msedge")

    errors = []
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    # 0) 工艺详情页（打印校准按钮）
    pg.goto(f"{BASE}/static/index.html#/processes/{pid}")
    pg.wait_for_selector("#btn-calibration", timeout=15000)
    pg.wait_for_timeout(1200)
    pg.screenshot(path=str(OUT / "process-detail.png"), full_page=False)

    # 1) 向导步骤 1：生成校准版
    pg.click("#btn-calibration")
    pg.wait_for_selector(".cal-gen", timeout=8000)
    pg.click(".cal-gen")
    pg.wait_for_selector(".cal-pdf", timeout=30000)
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "cal-wizard-step1.png"), full_page=False)
    pg.click(".cal-next1")

    # 2) 步骤 2：导入两遍测量 → 报告（深暗会话数据走完整 UI 流）
    pg.wait_for_selector("#cal-files", timeout=8000)
    (OUT / "m1.txt").write_bytes(tsv_from_labs(dark))
    (OUT / "m2.txt").write_bytes(tsv_from_labs(dark))
    pg.set_input_files("#cal-files", [str(OUT / "m1.txt"), str(OUT / "m2.txt")])
    pg.click(".cal-import")
    pg.wait_for_selector("#cal-report table", timeout=60000)
    pg.wait_for_timeout(900)
    scroll_main_to(pg, "#cal-report")
    pg.screenshot(path=str(OUT / "cal-wizard-step2.png"), full_page=False)
    pg.click(".cal-next2")

    # 3) 步骤 3：应用（confirmDanger 确认）→ bias chips + 导出按钮
    pg.wait_for_selector(".cal-apply", timeout=8000)
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(OUT / "cal-wizard-step3.png"), full_page=False)
    pg.click(".cal-apply")
    pg.wait_for_selector('[data-cd="ok"]', timeout=8000)
    pg.click('[data-cd="ok"]')
    pg.wait_for_selector("#cal-apply-result .badge-green", timeout=20000)
    pg.wait_for_timeout(600)
    pg.screenshot(path=str(OUT / "cal-applied.png"), full_page=False)
    pg.close()

    # 4) 监控页 QC 卡：校准状态行（选深暗会话）+ 暗色
    m = browser.new_page(viewport={"width": 1280, "height": 900})
    m.on("pageerror", lambda e: errors.append(str(e)))
    m.goto(f"{BASE}/static/index.html#/monitor")
    m.wait_for_selector("#qc-ink-session", timeout=20000)
    m.select_option("#qc-ink-session", value=sid_dark)
    m.wait_for_timeout(2500)
    scroll_main_to(m, "#qc-ink-card")
    m.eval_on_selector(".main", "el => el.scrollTop -= 48")
    m.wait_for_timeout(500)
    m.screenshot(path=str(OUT / "monitor-cal-line.png"), full_page=False)
    m.evaluate("localStorage.setItem('theme','dark'); document.documentElement.dataset.theme='dark'; window.dispatchEvent(new Event('themechange'))")
    m.wait_for_timeout(900)
    m.screenshot(path=str(OUT / "monitor-dark-cal.png"), full_page=False)

    # 5) 375px：QC 卡校准行 + 向导步骤 2 表格
    m.close()
    s = browser.new_page(viewport={"width": 375, "height": 812})
    s.on("pageerror", lambda e: errors.append(str(e)))
    s.goto(f"{BASE}/static/index.html#/monitor")
    s.wait_for_selector("#qc-ink-session", timeout=20000)
    s.select_option("#qc-ink-session", value=sid_dark)
    s.wait_for_timeout(2200)
    scroll_main_to(s, "#qc-ink-card")
    s.wait_for_timeout(400)
    s.screenshot(path=str(OUT / "cal-wizard-375.png"), full_page=False)
    s.close()

    browser.close()
    fatal = [e for e in errors if "favicon" not in e.lower()]
    print("console/page errors:", fatal[:5] if fatal else "none")

print("shots done ->", OUT)
server.should_exit = True
