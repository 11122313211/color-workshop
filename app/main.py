"""色彩管理闭环 AI 平台 —— FastAPI 应用入口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .routers import (
    router_devices,
    router_measurements,
    router_color_checker,
    router_convert,
    router_correction,
    router_delta_e,
    router_closed_loop,
    router_visualization,
    router_workflow,
    router_system,
    router_processes,
    router_recipes,
    router_files,
    router_qc,
    router_signoffs,
    router_displays,
    router_passports,
    router_spectral,
)

app = FastAPI(
    title="色彩工坊 · 工艺色彩证据平台",
    description="面向印前的数值优化与光谱证据闭环：测—析—校—验—迭代端到端自动化",
    version="0.10.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册所有路由
app.include_router(router_system)
app.include_router(router_processes)
app.include_router(router_recipes)
app.include_router(router_files)
app.include_router(router_qc)
app.include_router(router_signoffs)
app.include_router(router_displays)
app.include_router(router_passports)
app.include_router(router_spectral)
app.include_router(router_devices)
app.include_router(router_measurements)
app.include_router(router_color_checker)
app.include_router(router_convert)
app.include_router(router_correction)
app.include_router(router_delta_e)
app.include_router(router_closed_loop)
app.include_router(router_visualization)
app.include_router(router_workflow)

# 静态文件（前端页面）
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")
