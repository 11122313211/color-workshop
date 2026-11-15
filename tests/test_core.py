"""色彩管理闭环 AI 平台测试。

运行: python -m pytest tests/test_core.py -v
"""

import io

import numpy as np
from PIL import Image

from app.algorithms import (
    convert_color,
    delta_e,
    delta_e_from_rgb,
    gamut_map_color,
    compute_gamut_volume,
    gamut_coverage,
    create_srgb_profile,
    profile_to_profile,
)
from app.ai import (
    detect_color_checker,
    closed_loop_correct,
    delta_e_from_rgb as ai_delta_e,
)


def test_convert_color():
    """色彩空间转换。"""
    # sRGB -> Lab
    lab = convert_color([128, 100, 60], "srgb", "lab")
    assert len(lab) == 3
    assert lab[0] > 0 and lab[0] < 100

    # sRGB -> Display P3
    p3 = convert_color([255, 0, 0], "srgb", "dci-p3")
    assert len(p3) == 3
    assert p3[0] > 0

    # sRGB -> Adobe RGB
    adobe = convert_color([0.5, 0.3, 0.2], "srgb", "adobe-rgb")
    assert len(adobe) == 3

    print("[PASS] 色彩空间转换测试通过")


def test_delta_e():
    """色差计算。"""
    # 相同颜色 ΔE = 0
    assert delta_e([50, 20, 30], [50, 20, 30]) == 0.0

    # 不同颜色 ΔE > 0
    de = delta_e([50, 20, 30], [52, 22, 31])
    assert de > 0

    # CIEDE2000 vs CIE1976 都可用
    de2000 = delta_e([50, 20, 30], [52, 22, 31], "cie2000")
    de1976 = delta_e([50, 20, 30], [52, 22, 31], "cie1976")
    assert de2000 > 0 and de1976 > 0

    # RGB 直接色差
    de_rgb = delta_e_from_rgb([255, 0, 0], [250, 5, 5])
    assert de_rgb > 0

    print("[PASS] 色差计算测试通过")


def test_gamut():
    """色域计算与映射。"""
    # 色域体积：P3 > sRGB
    vol_srgb = compute_gamut_volume("srgb")
    vol_p3 = compute_gamut_volume("dci-p3")
    assert vol_p3 > vol_srgb

    # 覆盖率
    cov = gamut_coverage("dci-p3", "srgb")
    assert cov > 0

    # 色域映射：色域外颜色被压缩
    outside = gamut_map_color([80, 90, -70], "srgb")
    assert len(outside) == 3

    # 色域内颜色保持
    inside = gamut_map_color([50, 20, 30], "srgb")
    assert len(inside) == 3

    print("[PASS] 色域计算与映射测试通过")


def test_closed_loop():
    """闭环校正迭代。"""
    result = closed_loop_correct(
        [180, 130, 85],  # 初始测量
        [200, 150, 100],  # 目标
        tolerance=2.0,
        max_iterations=10,
    )
    assert result["converged"] is True
    assert result["final_delta_e"] <= 2.0
    assert result["iterations_used"] >= 1

    # 迭代轨迹应记录每次的 ΔE
    assert len(result["iterations"]) == result["iterations_used"]
    for it in result["iterations"]:
        assert "iteration" in it
        assert "delta_e" in it
        assert "measured_rgb" in it

    print(f"[PASS] 闭环校正测试通过（收敛于 ΔE={result['final_delta_e']}，{result['iterations_used']}次迭代）")


def test_color_checker():
    """色卡检测。"""
    # 生成模拟 ColorChecker 色卡
    colors = [
        [115, 115, 115], [150, 150, 150], [200, 200, 200], [242, 242, 242],
        [130, 60, 30], [100, 150, 210], [70, 130, 180], [40, 110, 70],
        [190, 50, 40], [240, 190, 40], [250, 250, 160], [120, 190, 150],
        [180, 50, 130], [80, 150, 120], [230, 150, 60], [150, 100, 50],
        [200, 80, 100], [60, 100, 170], [80, 70, 140], [120, 60, 160],
        [180, 160, 140], [220, 200, 180], [100, 90, 110], [50, 50, 60],
    ]
    patch = 80
    margin = 30
    rows, cols = 4, 6
    w = cols * (patch + margin) + margin
    h = rows * (patch + margin) + margin
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = 30
    for i, color in enumerate(colors):
        row = i // cols
        col = i % cols
        y = margin + row * (patch + margin)
        x = margin + col * (patch + margin)
        img[y:y+patch, x:x+patch] = color

    pil_img = Image.fromarray(img, mode="RGB")
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    patches, detected, quality = detect_color_checker(
        image_bytes, expected_patches=24, space="srgb"
    )
    assert detected is True
    assert len(patches) >= 20  # 至少检测到 20 个色块
    assert quality > 80

    print(f"[PASS] 色卡检测测试通过（检测到 {len(patches)} 个色块，质量评分 {quality}）")


def test_profile():
    """ICC profile 生成与转换。"""
    prof = create_srgb_profile()
    assert prof is not None

    # 图像转换
    img = Image.new("RGB", (4, 4), (200, 150, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    converted = profile_to_profile(buf.getvalue(), "srgb", "dci-p3")
    assert len(converted) > 0

    print("[PASS] ICC profile 测试通过")


if __name__ == "__main__":
    print("=" * 50)
    print("色彩管理闭环 AI 平台 - 核心功能测试")
    print("=" * 50)
    test_convert_color()
    test_delta_e()
    test_gamut()
    test_closed_loop()
    test_color_checker()
    test_profile()
    print("=" * 50)
    print("全部测试通过!")
    print("=" * 50)
