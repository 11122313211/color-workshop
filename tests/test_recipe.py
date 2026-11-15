"""配方反查引擎测试。

运行: python -m pytest tests/test_recipe.py -v
CMYK ICC 取自 Windows 系统 profile（RSWOP / JapanColor）；无系统 profile 时跳过。
"""

import io
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.processes import detect_device_space
from app.services.recipe import (
    CHANNEL_NAMES,
    RecipeStore,
    predict_recipe,
    run_verification,
)

WIN_COLOR_DIR = Path(r"C:/Windows/System32/spool/drivers/color")


def _load_cmyk_icc() -> bytes:
    """加载系统 CMYK profile；不存在则跳过测试。"""
    for name in ("RSWOP.icm", "JapanColor2001Coated.icc"):
        p = WIN_COLOR_DIR / name
        if p.exists():
            return p.read_bytes()
    import pytest
    pytest.skip("系统无 CMYK ICC profile（RSWOP/JapanColor），跳过 CMYK 反查测试")


def _load_rgb_icc() -> bytes:
    for name in ("sRGB Color Space Profile.icm", "AdobeRGB1998.icc"):
        p = WIN_COLOR_DIR / name
        if p.exists():
            return p.read_bytes()
    import pytest
    pytest.skip("系统无 RGB ICC profile，跳过 RGB 反查测试")


def test_detect_device_space():
    """ICC 头部签名探测设备色彩空间。"""
    icc = _load_cmyk_icc()
    assert detect_device_space(icc) == "CMYK"
    rgb = _load_rgb_icc()
    assert detect_device_space(rgb) == "RGB"
    print("[PASS] 设备色彩空间探测")


def test_forward_physical_sanity():
    """正向预测物理合理性：CMYK 四原色的 Lab 方向正确。"""
    icc = _load_cmyk_icc()
    from app.services.recipe import _build_forward

    forward = _build_forward(icc, 4)
    pts = np.array([
        [0.0, 0.0, 0.0, 0.0],  # 纸色 -> L≈100
        [0.0, 1.0, 1.0, 0.0],  # M+Y 红 -> a>0
        [1.0, 0.0, 0.0, 0.0],  # C 青 -> a<0, b<0
        [0.0, 0.0, 1.0, 0.0],  # Y 黄 -> b>0
    ])
    labs = forward(pts)
    assert labs[0][0] > 95, "纸色 L 应接近 100"
    assert labs[1][1] > 30, "红 a* 应为正且较高"
    assert labs[2][1] < 0 and labs[2][2] < 0, "青 a*/b* 应为负"
    assert labs[3][2] > 40, "黄 b* 应为正且较高"
    print("[PASS] 正向预测物理合理性")


def test_predict_recipe_cmyk():
    """LAB→CMYK 反查：色域内目标应收敛到 ΔE<3。"""
    icc = _load_cmyk_icc()
    targets = [
        [50.0, 20.0, 5.0],    # 中红
        [50.0, -20.0, -25.0],  # 青
        [60.0, 30.0, 40.0],   # 橙
        [85.0, -8.0, 30.0],   # 浅暖黄
    ]
    for target in targets:
        r = predict_recipe(icc, "CMYK", target, tolerance=2.0)
        assert set(r["recipe"].keys()) == {"C", "M", "Y", "K"}
        assert all(0.0 <= v <= 100.0 for v in r["recipe"].values())
        assert r["delta_e"] < 3.0, f"目标 {target} 预测 ΔE={r['delta_e']} 应优于 3"
        assert r["converged"] is True
        assert len(r["iterations"]) >= 1
        assert r["predictions"] > 0
    print("[PASS] LAB→CMYK 反查收敛（4 个色域内目标全部 ΔE<3）")


def test_predict_recipe_rgb_process():
    """RGB 工艺反查（3 通道引擎泛化）。"""
    icc = _load_rgb_icc()
    # sRGB 色域内的目标：Lab 50,20,5 ≈ 暗红
    r = predict_recipe(icc, "RGB", [50.0, 20.0, 5.0], tolerance=2.0)
    assert set(r["recipe"].keys()) == {"R", "G", "B"}
    assert r["delta_e"] < 3.0
    print(f"[PASS] RGB 工艺反查（ΔE={r['delta_e']}）")


def test_predict_recipe_out_of_gamut():
    """色域外目标：正常返回最优可印配方，不收敛但不应崩溃。"""
    icc = _load_cmyk_icc()
    r = predict_recipe(icc, "CMYK", [55.0, -80.0, 25.0], tolerance=2.0)  # 高饱和绿，胶印印不出
    assert r["converged"] is False
    assert r["delta_e"] > 2.0
    # 仍是合法配方
    assert all(0.0 <= v <= 100.0 for v in r["recipe"].values())
    print(f"[PASS] 色域外目标返回最优可印配方（ΔE={r['delta_e']}，不虚假收敛）")


def test_verification_closed_loop():
    """实测验证闭环：模型误差补偿应改善 ΔE。"""
    icc = _load_cmyk_icc()
    r = predict_recipe(icc, "CMYK", [50.0, 20.0, 5.0], tolerance=2.0)
    doc = {
        "target_lab": r["target_lab"],
        "predicted_lab": r["predicted_lab"],
        "method": "cie2000",
        "tolerance": 2.0,
    }
    # 模拟打印实测：预测值 + 偏移
    p = r["predicted_lab"]
    measured = [p[0] + 3.0, p[1] - 2.0, p[2] + 1.5]
    ver = run_verification(icc, "CMYK", doc, measured)
    assert ver["delta_e_corrected"] < ver["delta_e_measured"], "闭环修正应改善 ΔE"
    assert ver["improved"] is True
    print(f"[PASS] 实测验证闭环（dE {ver['delta_e_measured']} -> {ver['delta_e_corrected']}）")


def test_recipe_store_persistence():
    """配方记录落盘与读取。"""
    store = RecipeStore()
    proc = {"id": "testproc", "name": "测试工艺", "icc_meta": {"device_space": "CMYK"}}
    result = {
        "target_lab": [50.0, 20.0, 5.0],
        "predicted_lab": [49.8, 20.0, 5.0],
        "recipe": {"C": 6.2, "M": 46.9, "Y": 18.8, "K": 40.6},
        "delta_e": 0.196,
        "iterations": [0.98, 0.2],
        "predictions": 100,
        "lut_points": 83521,
        "method": "cie2000",
        "tolerance": 2.0,
        "converged": True,
    }
    doc = store.create(proc, result, label="测试")
    assert store.get(doc["id"]) is not None
    docs = store.list("testproc")
    assert any(d["id"] == doc["id"] for d in docs)
    assert store.delete(doc["id"]) is True
    assert store.get(doc["id"]) is None
    print("[PASS] 配方记录落盘存储")


if __name__ == "__main__":
    print("=" * 50)
    print("配方反查引擎测试")
    print("=" * 50)
    test_detect_device_space()
    test_forward_physical_sanity()
    test_predict_recipe_cmyk()
    test_predict_recipe_rgb_process()
    test_predict_recipe_out_of_gamut()
    test_verification_closed_loop()
    test_recipe_store_persistence()
    print("全部测试通过!")
