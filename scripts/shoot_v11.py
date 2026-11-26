"""V0.11 视觉验收：护照历史、数据维护、光谱 200% / 375px / 暗色。

用法:
    python scripts/shoot_v11.py
    COLORFORGE_V11_BASE_URL=http://127.0.0.1:8799 python scripts/shoot_v11.py

默认连接 ``http://127.0.0.1:2354``，截图输出到 ``.tmp/shots-v11``。
脚本通过公开 HTTP API 造数，结束时按 V0.11 协议归档并永久清理测试工艺。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("COLORFORGE_V11_BASE_URL", "http://127.0.0.1:2354").rstrip("/")
OUT = Path(os.environ.get("COLORFORGE_V11_SHOTS_DIR", ROOT / ".tmp" / "shots-v11"))
OUT.mkdir(parents=True, exist_ok=True)

client = httpx.Client(base_url=BASE, timeout=120)


def check(response: httpx.Response, label: str) -> dict:
    if response.status_code != 200:
        raise RuntimeError(
            f"{label} 失败: HTTP {response.status_code} {response.text[:300]}"
        )
    return response.json()


def wait_for_server() -> None:
    last_error = ""
    for _ in range(60):
        try:
            health = client.get("/api/health")
            if health.status_code == 200:
                return
            last_error = health.text[:200]
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise SystemExit(
        f"无法连接 {BASE}/api/health：{last_error}\n"
        "请先启动 V0.11 服务，或设置 COLORFORGE_V11_BASE_URL。"
    )


wait_for_server()

icc = (ROOT / "tests" / "fixtures" / "icc" / "testbed_cmyk.icc").read_bytes()
process = check(
    client.post(
        "/api/processes",
        params={
            "name": "V0.11 视觉验收演示",
            "paper": "铜版纸 157g",
            "ink": "标准四色",
            "filename": "testbed_cmyk.icc",
            "measurement_condition": "M1",
        },
        content=icc,
        headers={"Content-Type": "application/octet-stream"},
    ),
    "创建工艺",
)
pid = process["id"]
sessions: list[str] = []


def cleanup() -> None:
    for sid in sessions:
        try:
            client.delete("/api/qc/measurements", params={"session_id": sid})
        except httpx.HTTPError:
            pass
    for method, path, kwargs in (
        ("DELETE", f"/api/processes/{pid}", {}),
        (
            "DELETE",
            f"/api/processes/{pid}/permanent",
            {"params": {"confirm": pid}},
        ),
    ):
        try:
            client.request(method, path, **kwargs)
        except httpx.HTTPError:
            pass


pair = json.loads(
    (ROOT / "tests" / "fixtures" / "spectral" / "metameric_pair.json").read_text(
        encoding="utf-8"
    )
)
SPEC_A = {int(key): float(value) for key, value in pair["a"].items()}
SPEC_B = {int(key): float(value) for key, value in pair["b"].items()}
WAVELENGTHS = list(range(380, 731, 10))


def cgats(patches: list[tuple[str, dict[int, float]]]) -> bytes:
    fields = "\t".join(["SampleID", "SAMPLE_NAME"] + [f"nm{w}" for w in WAVELENGTHS])
    head = (
        'INSTRUMENTATION\t"Eye-One Pro（V0.11 视觉验收）"\n'
        'ILLUMINATION_NAME\t"D50"\n'
        'OBSERVER_ANGLE\t"2"\n'
        f"NUMBER_OF_FIELDS\t{len(WAVELENGTHS) + 2}\n"
        "BEGIN_DATA_FORMAT\n" + fields + "\nEND_DATA_FORMAT\n"
        f"NUMBER_OF_SETS\t{len(patches)}\nBEGIN_DATA\n"
    )
    rows = []
    for label, spectral in patches:
        values = [spectral.get(w, 0.0) for w in WAVELENGTHS]
        rows.append(f"1\t{label}\t" + "\t".join(f"{value:.4f}" for value in values))
    return (head + "\n".join(rows) + "\nEND_DATA\n").encode("utf-8")


def verified_session(instrument: str, patches: list[tuple[str, dict[int, float]]]) -> str:
    payload = cgats(patches)
    first = check(
        client.post(
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
        ),
        f"导入 {instrument}",
    )
    sid = first["session"]["id"]
    sessions.append(sid)
    second = check(
        client.post(
            "/api/qc/measurements/import",
            params={
                "filename": f"{instrument}-r2.txt",
                "session_id": sid,
                "repeat_mode": "repeat",
                "process_id": pid,
            },
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        ),
        f"复测 {instrument}",
    )
    if second["session"]["stats"]["verified"] < 2:
        raise RuntimeError(f"{instrument} 未形成 verified 复测对")
    return sid


def click_by_text(page, selector: str, text: str) -> None:
    locator = page.locator(selector).filter(has_text=text).first
    if locator.count() == 0:
        locator = page.get_by_role("button", name=text).first
    locator.wait_for(state="visible", timeout=15000)
    locator.click()


def scroll_main_to(page, selector: str, top_below: float = 100, attempts: int = 12) -> None:
    for _ in range(attempts):
        page.eval_on_selector(
            selector,
            "el => el.scrollIntoView({block:'start', behavior:'instant'})",
        )
        page.wait_for_timeout(250)
        top = page.eval_on_selector(selector, "el => el.getBoundingClientRect().top")
        if top is not None and top <= top_below:
            break


def close_modal(page) -> None:
    close = page.locator(".modal [data-close]").first
    if close.count():
        close.click()
    else:
        page.keyboard.press("Escape")
    page.wait_for_timeout(300)


errors: list[str] = []
screens: list[str] = []
keyboard_checks: list[str] = []
try:
    verified_session("V0.11 Spectra A", [("L1", SPEC_A), ("L2", SPEC_B)])
    verified_session("V0.11 Spectra B", [("L1", SPEC_B), ("L2", SPEC_A)])

    p1 = check(client.post(f"/api/processes/{pid}/passport"), "护照 v1")
    check(
        client.patch(
            f"/api/processes/{pid}", json={"measurement_condition": "M2"}
        ),
        "切换测量条件",
    )
    p2 = check(client.post(f"/api/processes/{pid}/passport"), "护照 v2")
    if p2["version"] <= p1["version"]:
        raise RuntimeError("护照历史未生成新版本")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, channel="msedge")

        desktop = browser.new_page(viewport={"width": 1280, "height": 900})
        desktop.on("pageerror", lambda exc: errors.append(str(exc)))
        desktop.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        desktop.goto(f"{BASE}/static/index.html#/processes/{pid}")
        desktop.wait_for_selector("#passport-slot .passport-card", timeout=30000)
        desktop.wait_for_timeout(700)
        history_button = desktop.locator('#passport-slot [data-pp="versions"]').first
        history_button.focus()
        click_by_text(desktop, "#passport-slot button", "版本历史")
        desktop.wait_for_selector(".modal", timeout=10000)
        desktop.wait_for_selector(".modal .passport-version-row", timeout=10000)
        desktop.get_by_text("版本历史", exact=False).first.wait_for(timeout=10000)
        desktop.wait_for_timeout(500)
        desktop.screenshot(path=str(OUT / "passport-history.png"), full_page=False)
        screens.append("passport-history.png")
        desktop.keyboard.press("Tab")
        if not desktop.evaluate(
            "Boolean(document.activeElement && document.activeElement.closest('.modal'))"
        ):
            raise RuntimeError("Tab 未把焦点保持在模态内")
        keyboard_checks.append("Tab 圈闭")
        desktop.keyboard.press("Shift+Tab")
        if not desktop.evaluate(
            "Boolean(document.activeElement && document.activeElement.closest('.modal'))"
        ):
            raise RuntimeError("Shift+Tab 未把焦点保持在模态内")
        keyboard_checks.append("Shift+Tab 圈闭")
        desktop.keyboard.press("Escape")
        desktop.wait_for_selector(".modal", state="detached", timeout=5000)
        if not desktop.evaluate(
            "document.activeElement && document.activeElement.dataset.pp === 'versions'"
        ):
            raise RuntimeError("Esc 关闭后未把焦点还原到版本历史按钮")
        keyboard_checks.extend(("Esc 关闭", "焦点还原"))
        desktop.close()

        maintenance = browser.new_page(viewport={"width": 1280, "height": 900})
        maintenance.on("pageerror", lambda exc: errors.append(str(exc)))
        maintenance.goto(f"{BASE}/static/index.html#/tools")
        maintenance.wait_for_selector("#tool-seg", timeout=20000)
        click_by_text(maintenance, "#tool-seg button", "数据维护")
        maintenance.wait_for_timeout(900)
        maintenance.screenshot(path=str(OUT / "integrity-tools.png"), full_page=False)
        screens.append("integrity-tools.png")
        maintenance.close()

        spectral = browser.new_page(viewport={"width": 1440, "height": 1100})
        spectral.on("pageerror", lambda exc: errors.append(str(exc)))
        spectral.goto(f"{BASE}/static/index.html#/monitor")
        spectral.wait_for_selector("#qc-spectral-card .spectral-grid", timeout=45000)
        spectral.wait_for_timeout(800)
        scroll_main_to(spectral, "#qc-spectral-card")
        spectral.click('[data-spectral-row="0"]')
        spectral.wait_for_selector("#spectral-modal-canvas", timeout=10000)
        spectral.wait_for_timeout(600)
        spectral.evaluate("document.documentElement.style.zoom = '2'")
        spectral.wait_for_timeout(700)
        spectral.screenshot(path=str(OUT / "spectral-curves-200.png"), full_page=False)
        screens.append("spectral-curves-200.png")
        spectral.evaluate(
            "localStorage.setItem('theme','dark');"
            "document.documentElement.dataset.theme='dark';"
            "window.dispatchEvent(new CustomEvent('themechange',{detail:{theme:'dark'}}))"
        )
        spectral.wait_for_timeout(900)
        spectral.screenshot(
            path=str(OUT / "spectral-curves-200-dark.png"), full_page=False
        )
        screens.append("spectral-curves-200-dark.png")
        spectral.close()

        dark = browser.new_page(viewport={"width": 1280, "height": 900})
        dark.add_init_script("localStorage.setItem('theme','dark')")
        dark.on("pageerror", lambda exc: errors.append(str(exc)))
        dark.goto(f"{BASE}/static/index.html#/processes/{pid}")
        dark.wait_for_selector("#passport-slot .passport-card", timeout=30000)
        dark.wait_for_timeout(800)
        click_by_text(dark, "#passport-slot button", "版本历史")
        dark.wait_for_selector(".modal", timeout=10000)
        dark.wait_for_timeout(600)
        dark.screenshot(path=str(OUT / "passport-history-dark.png"), full_page=False)
        screens.append("passport-history-dark.png")
        dark.close()

        mobile = browser.new_page(viewport={"width": 375, "height": 812})
        mobile.on("pageerror", lambda exc: errors.append(str(exc)))
        mobile.goto(f"{BASE}/static/index.html#/tools")
        mobile.wait_for_selector("#tool-seg", timeout=20000)
        click_by_text(mobile, "#tool-seg button", "数据维护")
        mobile.wait_for_timeout(900)
        mobile.screenshot(path=str(OUT / "integrity-tools-375.png"), full_page=False)
        screens.append("integrity-tools-375.png")
        mobile.goto(f"{BASE}/static/index.html#/processes/{pid}")
        mobile.wait_for_selector("#passport-slot .passport-card", timeout=30000)
        mobile.wait_for_timeout(400)
        click_by_text(mobile, "#passport-slot button", "版本历史")
        mobile.wait_for_selector(".modal", timeout=10000)
        mobile.wait_for_timeout(600)
        mobile.screenshot(path=str(OUT / "passport-history-375.png"), full_page=False)
        screens.append("passport-history-375.png")
        mobile.close()

        # V0.11 生命周期 UI 回归：归档 → 只读 → 恢复 → 永久删除。
        # 放在截图之后，避免归档态影响上面的视觉验收。
        lifecycle = browser.new_page(viewport={"width": 768, "height": 900})
        lifecycle.on("pageerror", lambda exc: errors.append(str(exc)))
        lifecycle.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        lifecycle.goto(f"{BASE}/static/index.html#/processes/{pid}")
        lifecycle.wait_for_selector("#btn-archive", timeout=30000)
        lifecycle.click("#btn-archive")
        lifecycle.fill("#archive-actor", "V0.11 截图验收")
        lifecycle.fill("#archive-reason", "生命周期 UI 回归")
        lifecycle.click("#archive-confirm")
        lifecycle.wait_for_selector("#proc-show-archived", timeout=15000)
        lifecycle.click("#proc-show-archived")
        lifecycle.wait_for_selector(".process-card.is-archived", timeout=15000)
        lifecycle.click(".process-card.is-archived")
        lifecycle.wait_for_selector(".archive-banner", timeout=15000)
        if not lifecycle.locator("#btn-use").is_disabled():
            errors.append("归档详情未禁用预测按钮")
        lifecycle.click("#btn-restore-process")
        lifecycle.wait_for_selector(".archive-banner", state="detached", timeout=15000)
        if lifecycle.locator("#btn-use").is_disabled():
            errors.append("恢复后预测按钮仍禁用")

        check(
            client.post(
                f"/api/processes/{pid}/archive",
                json={"reason": "生命周期 UI 回归", "actor": "V0.11 截图验收"},
            ),
            "生命周期 UI 前置归档",
        )
        # 同一 hash URL 的 goto 不保证重新渲染，显式 reload 读取归档态。
        lifecycle.reload()
        lifecycle.wait_for_selector(".archive-banner", timeout=30000)
        lifecycle.click("#btn-permanent-delete")
        lifecycle.wait_for_selector("#dependency-delete", timeout=15000)
        lifecycle.click("#dependency-delete")
        lifecycle.wait_for_selector(".modal-confirm", timeout=10000)
        lifecycle.click('[data-cd="ok"]')
        lifecycle.wait_for_selector("#proc-show-archived", timeout=20000)
        if client.get(f"/api/processes/{pid}").status_code != 404:
            errors.append("永久删除后工艺仍可读取")
        keyboard_checks.append("归档/恢复/永久删除 UI")
        lifecycle.close()
        browser.close()
finally:
    cleanup()

fatal = [error for error in errors if "favicon" not in error.lower()]
print("process:", pid)
print("passport versions:", p1["version"], "->", p2["version"])
print("keyboard:", " / ".join(keyboard_checks))
print("screens:", ", ".join(screens))
print("console/page errors:", fatal[:8] if fatal else "none")
print("shots done ->", OUT)
sys.exit(0 if not fatal else 1)
