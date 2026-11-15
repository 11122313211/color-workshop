"""色彩算法层：lcms2 + colour-science + coloraide 的编排封装。"""
from .core import (
    SUPPORTED_SPACES,
    RenderingIntent,
    convert_color,
    delta_e,
    delta_e_from_rgb,
    create_srgb_profile,
    load_profile_from_bytes,
    profile_to_profile,
    gamut_map_color,
    compute_gamut_volume,
    gamut_coverage,
)

__all__ = [
    "SUPPORTED_SPACES",
    "RenderingIntent",
    "convert_color",
    "delta_e",
    "delta_e_from_rgb",
    "create_srgb_profile",
    "load_profile_from_bytes",
    "profile_to_profile",
    "gamut_map_color",
    "compute_gamut_volume",
    "gamut_coverage",
]
