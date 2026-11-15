"""AI 层：色卡检测、ΔE 预测、闭环迭代引擎。"""
from .color_checker import (
    PatchDetection,
    detect_color_checker,
    generate_calibration_profile,
    predict_delta_e_after_correction,
    closed_loop_correct,
    delta_e_from_rgb,
    decode_base64_image,
)

__all__ = [
    "PatchDetection",
    "detect_color_checker",
    "generate_calibration_profile",
    "predict_delta_e_after_correction",
    "closed_loop_correct",
    "delta_e_from_rgb",
    "decode_base64_image",
]
