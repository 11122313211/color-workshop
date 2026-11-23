"""V0.10 视觉验收：工艺护照、签样护照门禁、光谱 / 同色异谱。

用法: python scripts/shoot_v10.py [输出目录]
产出:
  process-passport-card.png / passport-detail.png / passport-375.png
  recipe-device-values.png / monitor-spectral-card.png /
  monitor-spectral-dark.png / spectral-curves-modal.png / spectral-375.png

造数：CMYK 工艺 → 两个含光谱的 verified 会话（合成同色异谱对）→
校准轮次 → 生成护照 → 签样批准 → 配方预测。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".tmp" / f"cw_v10_shots_{uuid.uuid4().hex[:8]}"
OUT.mkdir(parents=True, exist_ok=True)
DATA_DIR = ROOT / ".tmp" / f"cw_v10_data_{uuid.uuid4().hex[:8]}"
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["COLORFORGE_DATA_DIR"] = str(DATA_DIR)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()

client = httpx.Client(base_url=BASE, timeout=120)
for _ in range(60):
    try:
        if client.get("/api/health").status_code == 200:
            break
    except httpx.HTTPError:
        pass
    time.sleep(0.25)


def check(response: httpx.Response, label: str) -> dict:
    if response.status_code != 200:
        raise RuntimeError(f"{label} 失败: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


icc = (ROOT / "tests" / "fixtures" / "icc" / "testbed_cmyk.icc").read_bytes()
process = check(client.post(
    "/api/processes",
    params={
        "name": "V0.10 工艺护照演示",
        "paper": "铜版纸 157g",
        "ink": "标准四色",
        "filename": "testbed_cmyk.icc",
        "measurement_condition": "M1",
    },
    content=icc,
    headers={"Content-Type": "application/octet-stream"},
), "创建工艺")
pid = process["id"]

pair = json.loads((ROOT / "tests" / "fixtures" / "spectral" / "metameric_pair.json").read_text(encoding="utf-8"))
A = {int(k): float(v) for k, v in pair["a"].items()}
B = {int(k): float(v) for k, v in pair["b"].items()}
WAVELENGTHS = list(range(380, 731, 10))


def cgats(patches: list[tuple[str, dict[int, float]]]) -> bytes:
    fields = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in WAVELENGTHS])
    head = (
        'INSTRUMENTATION\t"Eye-One Pro（合成验收）"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        f"NUMBER_OF_FIELDS\t{len(WAVELENGTHS) + 2}\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        f"NUMBER_OF_SETS\t{len(patches)}\nBEGIN_DATA\n"
    )
    rows = []
    for label, spectral in patches:
        values = [spectral.get(w, 0.0) for w in WAVELENGTHS]
        rows.append(f"1\t{label}\t" + "\t".join(f"{v:.4f}" for v in values))
    return (head + "\n".join(rows) + "\nEND_DATA\n").encode("utf-8")


def verified_session(instrument: str, patches: list[tuple[str, dict[int, float]]]) -> str:
    payload = cgats(patches)
    first = check(client.post(
        "/api/qc/measurements/import",
        params={
            "filename": f"{instrument}.txt",
            "instrument": instrument,
            "condition": "M1",
            "calibrated_at": "2026-11-23T09:30:00",
            "repeat_mode": "single",
            "process_id": pid,
        },
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    ), f"导入 {instrument}")
    sid = first["session"]["id"]
    second = check(client.post(
        "/api/qc/measurements/import",
        params={
            "filename": f"{instrument}-r2.txt",
            "session_id": sid,
            "repeat_mode": "repeat",
            "process_id": pid,
        },
        content=payload,
        headers={"Content-Type": "application/octet-stream"},
    ), f"复测 {instrument}")
    if second["session"]["stats"]["verified"] < 2:
        raise RuntimeError(f"{instrument} 未形成 verified 复测对: {second['session']['stats']}")
    return sid


sid_a = verified_session("Spectra Probe A", [("L1", A), ("L2", B)])
sid_b = verified_session("Spectra Probe B", [("L1", B), ("L2", A)])

from app.services.calibration_history import CalibrationStore  # noqa: E402

CalibrationStore().save_round(
    pid,
    process["icc_sha256"],
    sid_a,
    {
        "overall": "ok",
        "summary": {"max_de_expected": 1.1, "avg_de_expected": 0.5},
        "ink_suggestions": [
            {"channel": "C", "delta_pct": 0.2},
            {"channel": "M", "delta_pct": -0.1},
        ],
    },
    [0.0, 0.0, 0.0, 0.0],
    [0.01, -0.005, 0.0, 0.0],
)

check(client.post("/api/recipes/predict", json={
    "process_id": pid,
    "targets": [
        {"label": "护照红", "lab": [50.0, 20.0, 5.0]},
        {"label": "护照青", "lab": [50.0, -20.0, -25.0]},
    ],
    "tolerance": 2.0,
}), "配方预测")

passport = check(client.post(f"/api/processes/{pid}/passport"), "生成护照")
if passport["status"] != "valid":
    raise RuntimeError(f"演示护照应为 valid，实际 {passport['status']}: {passport['status_reasons']}")

signoff = check(client.post("/api/signoffs", json={
    "process_id": pid,
    "title": "工艺护照签样",
    "job_no": "V10-DEMO",
    "created_by": "色彩工坊",
    "evidence": {"file_name": "v10-proof.pdf"},
    "passport_id": passport["id"],
    "passport_hash": passport["passport_hash"],
}), "创建签样")
check(client.post(f"/api/signoffs/{signoff['id']}/submit", json={"actor": "设计组"}), "提交签样")
check(client.post(f"/api/signoffs/{signoff['id']}/approve", json={"actor": "客户"}), "批准签样")

from playwright.sync_api import sync_playwright  # noqa: E402


def scroll_main_to(page, selector: str, top_below: float = 100, attempts: int = 12) -> None:
    for _ in range(attempts):
        page.eval_on_selector(selector, "el => el.scrollIntoView({block:'start', behavior:'instant'})")
        page.wait_for_timeout(300)
        top = page.eval_on_selector(selector, "el => el.getBoundingClientRect().top")
        if top is not None and top <= top_below:
            break


errors: list[str] = []
with sync_playwright() as pw:
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:
        browser = pw.chromium.launch(headless=True, channel="msedge")

    desktop = browser.new_page(viewport={"width": 1280, "height": 900})
    desktop.on("pageerror", lambda exc: errors.append(str(exc)))
    desktop.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    desktop.goto(f"{BASE}/static/index.html#/processes/{pid}")
    desktop.wait_for_selector("#passport-slot .passport-card", timeout=20000)
    desktop.wait_for_selector("#signoff-slot .card", timeout=20000)
    desktop.wait_for_timeout(800)
    scroll_main_to(desktop, "#passport-slot .passport-card")
    desktop.eval_on_selector(".main", "el => el.scrollTop -= 36")
    desktop.wait_for_timeout(400)
    desktop.screenshot(path=str(OUT / "process-passport-card.png"), full_page=False)
    desktop.click('[data-pp="detail"]')
    desktop.wait_for_selector(".modal .kv", timeout=8000)
    desktop.wait_for_timeout(500)
    desktop.screenshot(path=str(OUT / "passport-detail.png"), full_page=False)
    desktop.click(".modal [data-close]")
    desktop.wait_for_timeout(300)

    desktop.goto(f"{BASE}/static/index.html#/recipes")
    desktop.wait_for_selector("#btn-predict", timeout=12000)
    desktop.click("#btn-predict")
    desktop.wait_for_selector("#result-0", timeout=90000)
    desktop.wait_for_timeout(700)
    scroll_main_to(desktop, "#result-0")
    desktop.eval_on_selector(".main", "el => el.scrollTop -= 50")
    desktop.wait_for_timeout(350)
    desktop.screenshot(path=str(OUT / "recipe-device-values.png"), full_page=False)
    desktop.close()

    monitor = browser.new_page(viewport={"width": 1280, "height": 900})
    monitor.on("pageerror", lambda exc: errors.append(str(exc)))
    monitor.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    monitor.goto(f"{BASE}/static/index.html#/monitor")
    monitor.wait_for_selector("#spectral-result .spectral-grid", timeout=30000)
    monitor.wait_for_timeout(900)
    scroll_main_to(monitor, "#qc-spectral-card")
    monitor.eval_on_selector(".main", "el => el.scrollTop -= 40")
    monitor.wait_for_timeout(400)
    monitor.screenshot(path=str(OUT / "monitor-spectral-card.png"), full_page=False)
    monitor.click('[data-spectral-row="0"]')
    monitor.wait_for_selector("#spectral-modal-canvas", timeout=8000)
    monitor.wait_for_timeout(500)
    monitor.screenshot(path=str(OUT / "spectral-curves-modal.png"), full_page=False)
    monitor.click(".modal [data-close]")
    monitor.evaluate(
        "localStorage.setItem('theme','dark');"
        "document.documentElement.dataset.theme='dark';"
        "window.dispatchEvent(new CustomEvent('themechange',{detail:{theme:'dark'}}))"
    )
    monitor.wait_for_timeout(1000)
    monitor.screenshot(path=str(OUT / "monitor-spectral-dark.png"), full_page=False)
    monitor.close()

    mobile = browser.new_page(viewport={"width": 375, "height": 812})
    mobile.on("pageerror", lambda exc: errors.append(str(exc)))
    mobile.goto(f"{BASE}/static/index.html#/monitor")
    mobile.wait_for_selector("#spectral-result .spectral-grid", timeout=30000)
    mobile.wait_for_timeout(700)
    scroll_main_to(mobile, "#qc-spectral-card")
    mobile.wait_for_timeout(400)
    mobile.screenshot(path=str(OUT / "spectral-375.png"), full_page=False)
    mobile.goto(f"{BASE}/static/index.html#/processes/{pid}")
    mobile.wait_for_selector("#passport-slot .passport-card", timeout=20000)
    mobile.wait_for_timeout(700)
    scroll_main_to(mobile, "#passport-slot .passport-card")
    mobile.wait_for_timeout(400)
    mobile.screenshot(path=str(OUT / "passport-375.png"), full_page=False)
    mobile.close()

    browser.close()

fatal = [err for err in errors if "favicon" not in err.lower()]
print("passport:", passport["status"], passport["version"], passport["passport_hash"][:12])
print("sessions:", sid_a, sid_b)
print("console/page errors:", fatal[:8] if fatal else "none")
print("shots done ->", OUT)

server.should_exit = True
