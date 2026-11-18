"""V0.8 视觉验收：校准轮次历史 + 轮次对比 + 收敛判定。

用法: python scripts/shoot_v08.py <输出目录>
产出: cal-history-card.png / cal-compare-card.png / cal-applied-round.png /
      monitor-cal-round.png / monitor-dark-round.png / cal-375.png
造数（全部合成）：铜版纸工艺 → 深暗会话 apply（第 1 轮）→ 改善会话
（向导 UI 导入）→ 对比卡 improved → apply（第 2 轮）→ 历史卡/监控行。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="cw_v08_shots_"))
OUT.mkdir(parents=True, exist_ok=True)

DATA_DIR = tempfile.mkdtemp(prefix="cw_v08_data_")
os.environ["COLORFORGE_DATA_DIR"] = DATA_DIR

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
import threading  # noqa: E402

t = threading.Thread(target=server.run, daemon=True)
t.start()
time.sleep(2.5)

c = httpx.Client(base_url=BASE, timeout=120)

icc = Path("tests/fixtures/icc/testbed_cmyk.icc").read_bytes()
r = c.post("/api/processes", params={"name": "铜版纸校准演示", "paper": "铜版纸", "ink": "四色",
                                     "filename": "testbed_cmyk.icc"},
           content=icc, headers={"Content-Type": "application/octet-stream"})
pid = r.json()["id"]

from app.services.calibration import build_reference  # noqa: E402

proc_doc = c.get("/api/processes").json()[0]
exp = {p["label"]: p["expected_lab"] for p in build_reference(proc_doc)["patches"]}

TSV_HEAD = "Color Name\tCIE Lab\t\t\tStatus T C\tM\tY\tK\r\n"


def tsv_from_labs(labs):
    rows = [f"{lb}\t{lab[0]:.2f}\t{lab[1]:.2f}\t{lab[2]:.2f}" for lb, lab in labs.items()]
    return (TSV_HEAD + "\r\n".join(rows) + "\r\n").encode()


def import_two(data):
    r = c.post("/api/qc/measurements/import",
               params={"filename": "cal-r1.txt", "instrument": "Xrite i1（校准演示）", "condition": "M1",
                       "calibrated_at": "2026-11-15T09:30:00", "repeat_mode": "single", "process_id": pid},
               content=data, headers={"Content-Type": "application/octet-stream"})
    sid = r.json()["session"]["id"]
    c.post("/api/qc/measurements/import",
           params={"filename": "cal-r2.txt", "session_id": sid, "repeat_mode": "repeat"},
           content=data, headers={"Content-Type": "application/octet-stream"})
    return sid


def make_labs(shift_k):
    labs = {k: list(v) for k, v in exp.items()}
    if shift_k:
        labs["K100"] = [exp["K100"][0] - shift_k, exp["K100"][1], exp["K100"][2]]
    return labs


# 第 1 轮：深暗（K −6）→ apply 落库
sid_dark = import_two(tsv_from_labs(make_labs(6.0)))
time.sleep(1.2)
r = c.post("/api/qc/calibration/apply", params={"session_id": sid_dark})
round1 = r.json()
print("round1:", round1["round_no"], round1["summary"]["max_de_expected"])

# 第 2 轮（改善，K −3）：经向导 UI 导入并应用
sid_good = import_two(tsv_from_labs(make_labs(3.0)))
time.sleep(1.2)

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

    # 1) 工艺详情页：校准历史卡（1 轮状态）
    pg.goto(f"{BASE}/static/index.html#/processes/{pid}")
    pg.wait_for_selector("#cal-history-slot .card", timeout=15000)
    pg.wait_for_timeout(1000)
    scroll_main_to(pg, "#cal-history-slot .card")
    pg.eval_on_selector(".main", "el => el.scrollTop -= 40")
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(OUT / "cal-history-card.png"), full_page=False)

    # 2) 向导：导入改善会话 → 报告带轮次对比卡（较上轮 improved）
    pg.click("#btn-calibration")
    pg.wait_for_selector(".cal-gen", timeout=8000)
    pg.click(".cal-gen")
    pg.wait_for_selector(".cal-next1", timeout=30000)
    pg.click(".cal-next1")
    pg.wait_for_selector("#cal-files", timeout=8000)
    (OUT / "g1.txt").write_bytes(tsv_from_labs(make_labs(3.0)))
    (OUT / "g2.txt").write_bytes(tsv_from_labs(make_labs(3.0)))
    pg.set_input_files("#cal-files", [str(OUT / "g1.txt"), str(OUT / "g2.txt")])
    pg.click(".cal-import")
    pg.wait_for_selector("#cal-report table", timeout=60000)
    pg.wait_for_timeout(900)
    scroll_main_to(pg, "#cal-report")
    pg.screenshot(path=str(OUT / "cal-compare-card.png"), full_page=False)
    pg.click(".cal-next2")

    # 3) 应用 → 第 2 轮 + verdict chip
    pg.wait_for_selector(".cal-apply", timeout=8000)
    pg.click(".cal-apply")
    pg.wait_for_selector('[data-cd="ok"]', timeout=8000)
    pg.click('[data-cd="ok"]')
    pg.wait_for_selector("#cal-apply-result .badge-green", timeout=20000)
    pg.wait_for_timeout(600)
    pg.screenshot(path=str(OUT / "cal-applied-round.png"), full_page=False)
    pg.close()

    # 4) 工艺详情页：历史卡 2 轮 + 收敛 chip
    d = browser.new_page(viewport={"width": 1280, "height": 900})
    d.on("pageerror", lambda e: errors.append(str(e)))
    d.goto(f"{BASE}/static/index.html#/processes/{pid}")
    d.wait_for_selector("#cal-history-slot .card", timeout=15000)
    d.wait_for_timeout(1200)
    scroll_main_to(d, "#cal-history-slot .card")
    d.eval_on_selector(".main", "el => el.scrollTop -= 40")
    d.wait_for_timeout(400)
    d.screenshot(path=str(OUT / "cal-history-card-2rounds.png"), full_page=False)
    d.close()

    # 5) 监控页：校准状态行（轮次 + 收敛）+ 暗色 + 375
    m = browser.new_page(viewport={"width": 1280, "height": 900})
    m.on("pageerror", lambda e: errors.append(str(e)))
    m.goto(f"{BASE}/static/index.html#/monitor")
    m.wait_for_selector("#qc-ink-session", timeout=20000)
    m.select_option("#qc-ink-session", value=sid_good)
    m.wait_for_timeout(2600)
    scroll_main_to(m, "#qc-ink-card")
    m.eval_on_selector(".main", "el => el.scrollTop -= 48")
    m.wait_for_timeout(500)
    m.screenshot(path=str(OUT / "monitor-cal-round.png"), full_page=False)
    m.evaluate("localStorage.setItem('theme','dark'); document.documentElement.dataset.theme='dark'; window.dispatchEvent(new Event('themechange'))")
    m.wait_for_timeout(900)
    m.screenshot(path=str(OUT / "monitor-dark-round.png"), full_page=False)
    m.close()

    s = browser.new_page(viewport={"width": 375, "height": 812})
    s.on("pageerror", lambda e: errors.append(str(e)))
    s.goto(f"{BASE}/static/index.html#/monitor")
    s.wait_for_selector("#qc-ink-session", timeout=20000)
    s.select_option("#qc-ink-session", value=sid_good)
    s.wait_for_timeout(2400)
    scroll_main_to(s, "#qc-ink-card")
    s.wait_for_timeout(400)
    s.screenshot(path=str(OUT / "cal-375.png"), full_page=False)
    s.close()

    browser.close()
    fatal = [e for e in errors if "favicon" not in e.lower()]
    print("console/page errors:", fatal[:5] if fatal else "none")

print("shots done ->", OUT)
server.should_exit = True
