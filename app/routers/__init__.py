"""路由层。"""
from .main import (
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
)
from .processes import router as router_processes
from .recipes import router as router_recipes
from .files import router as router_files
from .qc import router as router_qc
from .signoffs import router as router_signoffs
from .displays import router as router_displays
from .passport import router as router_passports
from .spectral import router as router_spectral

__all__ = [
    "router_devices",
    "router_measurements",
    "router_color_checker",
    "router_convert",
    "router_correction",
    "router_delta_e",
    "router_closed_loop",
    "router_visualization",
    "router_workflow",
    "router_system",
    "router_processes",
    "router_recipes",
    "router_files",
    "router_qc",
    "router_signoffs",
    "router_displays",
    "router_passports",
    "router_spectral",
]
